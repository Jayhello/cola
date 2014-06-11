#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Copyright (c) 2013 Qin Xuye <qin@qinxuye.me>

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Created on 2013-5-26

@author: Chine
'''

import re
import hashlib
import os
import multiprocessing
import multiprocessing.managers
import threading
import signal
import pprint

from cola.core.errors import ConfigurationError
from cola.core.utils import base58_encode, get_cpu_count, \
                            import_job_desc
from cola.core.mq import MessageQueue, MessageQueueRPCProxy
from cola.core.dedup import FileBloomFilterDeduper
from cola.core.unit import Bundle, Url
from cola.core.logs import get_logger
from cola.core.utils import is_windows
from cola.settings import Settings
from cola.functions.budget import BudgetApplyServer, ALLFINISHED
from cola.functions.speed import SpeedControlServer
from cola.functions.counter import CounterServer
from cola.job.container import Container

JOB_NAME_RE = re.compile(r'(\w| )+')
UNLIMIT_BLOOM_FILTER_CAPACITY = 1000000
NOTSTARTED, RUNNING, FINISHED = range(3)

class JobRunning(Exception): pass

class JobDescription(object):
    def __init__(self, name, url_patterns, opener_cls, user_conf, starts, 
                 unit_cls=None, login_hook=None, **kw):
        self.name = name
        if not JOB_NAME_RE.match(name):
            raise ConfigurationError('Job name can only contain alphabet, number and space.')
        self.uniq_name = self._get_uniq_name(self.name)
        
        self.url_patterns = url_patterns
        self.opener_cls = opener_cls
        
        self.user_conf = user_conf
        self.starts = starts
        self.login_hook = login_hook
        
        self.settings = Settings(user_conf=user_conf, **kw)
        self.unit_cls = unit_cls or \
            (Bundle if self.settings.job.mode == 'bundle' else Url)
        
    def _get_uniq_name(self, name):
        hash_val = hashlib.md5(name).hexdigest()[8:-8]
        return base58_encode(int(hash_val, 16))
        
    def add_urlpattern(self, url_pattern):
        self.url_patterns += url_pattern
        
def run_containers(n_containers, n_instances, working_dir, job_def_path, 
                   job_name, env, mq,
                   counter_server, budget_server, speed_server,
                   stopped, nonsuspend,
                   block=False, is_multi_process=False):    
    processes = []
    acc = 0
    for container_id in range(n_containers):
        n_tasks = n_instances / n_containers
        if container_id < n_instances % n_containers:
            n_tasks += 1
        container = Container(container_id, working_dir, job_def_path, job_name, 
                              env, mq, counter_server, budget_server, speed_server,
                              stopped, nonsuspend, n_tasks=n_tasks,
                              task_start_id=acc)
        if is_multi_process:
            process = multiprocessing.Process(target=container.run, 
                                              args=(True, ))
            process.start()
            processes.append(process)
        else:
            thread = threading.Thread(target=container.run, 
                                      args=(True, ))
            thread.start()
            processes.append(thread)
        acc += n_tasks
        
    if block:
        [process.join() for process in processes]
    return processes
        
class Job(object):
    def __init__(self, ctx, job_def_path, job_name=None, 
                 job_desc=None, working_dir=None, rpc_server=None,
                 manager=None):
        self.status = NOTSTARTED
        self.ctx = ctx
        self.shutdown_callbacks = []
        
        self.stopped = multiprocessing.Event()
        self.nonsuspend = multiprocessing.Event()
        self.nonsuspend.set()
        
        self.job_def_path = job_def_path
        self.job_name = job_name or self.job_desc.uniq_name
        self.working_dir = working_dir or os.path.join(self.ctx.working_dir, 
                                                       self.job_name)
        self.logger = get_logger(name='cola_job')
        self.job_desc = job_desc or import_job_desc(job_def_path)
            
        self.settings = self.job_desc.settings
        self.is_bundle = self.settings.job.mode == 'bundle'
                
        self.rpc_server = rpc_server
        
        self.n_instances = self.job_desc.settings.job.instances
        self.n_containers = min(get_cpu_count(), max(self.n_instances, 1)) \
                                if not is_windows() else 1
        self.is_multi_process = self.n_containers > 1 \
                                    if not is_windows() else False
        self.processes = []
            
        if not is_windows():
            self.manager = manager
        
        if not os.path.exists(self.working_dir):
            os.makedirs(self.working_dir)
        self.inited = False
        self.is_shutdown = False
        
    def init_deduper(self):
        base = 1 if not self.is_bundle else 1000
        size = self.job_desc.settings.job.size
        capacity = UNLIMIT_BLOOM_FILTER_CAPACITY
        if size > 0:
            capacity = max(base * size * 10, capacity)
        deduper_path = os.path.join(self.working_dir, 'dedup')
        deduper_cls = FileBloomFilterDeduper if not self.is_multi_process \
                        else self.manager.deduper
        self.deduper = deduper_cls(deduper_path, capacity)
        # register shutdown callback
        self.shutdown_callbacks.append(self.deduper.shutdown)
        
    def init_mq(self):
        mq_dir = os.path.join(self.working_dir, 'mq')
        copies = self.job_desc.settings.job.copies
        n_priorities = self.job_desc.settings.job.priorities
        
        kw = {'app_name': self.job_name, 'copies': copies, 
              'n_priorities': n_priorities, 'deduper': self.deduper}
        mq_cls = MessageQueue if not self.is_multi_process \
                    else self.manager.mq
        self.mq = mq_cls(mq_dir, None, self.ctx.addr, 
            self.ctx.addrs, **kw)
        if self.rpc_server:
            self.proxy = MessageQueueRPCProxy(self.mq.get_connection(), 
                                              self.rpc_server)
        # register shutdown callback
        self.shutdown_callbacks.append(self.mq.shutdown)
        
    def _init_function_servers(self):
        budget_dir = os.path.join(self.working_dir, 'budget')
        budget_cls =  BudgetApplyServer if not self.is_multi_process \
                        else self.manager.budget_server
        self.budget_server = budget_cls(budget_dir, self.settings, 
                                        None, self.job_name)
        if self.rpc_server:
            BudgetApplyServer.register_rpc(self.budget_server, self.rpc_server, 
                                           app_name=self.job_name)
        self.shutdown_callbacks.append(self.budget_server.shutdown)
        
        counter_dir = os.path.join(self.working_dir, 'counter')
        counter_cls = CounterServer if not self.is_multi_process \
                        else self.manager.counter_server
        self.counter_server = counter_cls(counter_dir, self.settings,
                                          None, self.job_name)
        if self.rpc_server:
            CounterServer.register_rpc(self.counter_server, self.rpc_server, 
                                       app_name=self.job_name)
        
        self.shutdown_callbacks.append(self.counter_server.shutdown)
        
        speed_dir = os.path.join(self.working_dir, 'speed')
        speed_cls = SpeedControlServer if not self.is_multi_process \
                        else self.manager.speed_server
        self.speed_server = speed_cls(speed_dir, self.settings,
                                      None, self.job_name,
                                      self.counter_server, self.ctx.ips)
        if self.rpc_server:
            SpeedControlServer.register_rpc(self.speed_server, self.rpc_server, 
                                            app_name=self.job_name)
        self.shutdown_callbacks.append(self.speed_server.shutdown)
        
    def init_functions(self):
        if self.ctx.is_local_mode:
            self._init_function_servers()
            self.counter_arg = self.counter_server
            self.budget_arg = self.budget_server
            self.speed_arg = self.speed_server
        else:
            self.counter_args, self.budget_args, self.speed_args = \
                tuple([self.ctx.master for _ in range(3)]) 
        
    def init(self):
        if self.inited:
            return
        
        self.lock_file = os.path.join(self.working_dir, 'lock')
        
        if os.path.exists(self.lock_file):
            raise JobRunning('The job has already started')
        open(self.lock_file, 'w').close()
        
        self.init_deduper()
        self.init_mq()
        self.init_functions()
        
        self.inited = True
        self.status = RUNNING
        
    def run(self, block=False):
        self.init()
        self.processes = run_containers(
            self.n_containers, self.n_instances, self.working_dir, 
            self.job_def_path, self.job_name, self.ctx.env, self.mq,
            self.counter_arg, self.budget_arg, self.speed_arg, 
            self.stopped, self.nonsuspend, is_multi_process=self.is_multi_process)
        if block:
            self.wait_for_stop()
            
    def wait_for_stop(self):
        [process.join() for process in self.processes]
        
    def shutdown(self):
        if 'main' not in multiprocessing.current_process().name.lower():
            return
        if self.is_shutdown:
            return
        else:
            self.is_shutdown = True
            
        try:
            self.stopped.set()
            
            self.wait_for_stop()
            
            # output counters
            if self.ctx.is_local_mode:
                self.logger.debug('Counters during running:')
                self.logger.debug(pprint.pformat(self.counter_server.output(), 
                                                 width=1))
            self.logger.debug('Processing shutting down')
            
            for cb in self.shutdown_callbacks:
                cb()
            if hasattr(self, 'manager'):
                self.manager.shutdown()
            self.status = FINISHED
            self.logger.debug('Shutdown finished')
        finally:
            if os.path.exists(self.lock_file):
                os.remove(self.lock_file)
            
    def get_status(self):
        if self.ctx.is_local_mode and self.status == RUNNING and \
            self.budget_server.get_status() == ALLFINISHED:
            return FINISHED
        return self.status
            
    def suspend(self):
        self.nonsuspend.clear()
        
    def resume(self):
        self.nonsuspend.set()