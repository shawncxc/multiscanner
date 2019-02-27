#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from __future__ import (absolute_import, division, unicode_literals, with_statement)

import argparse
import logging
import multiprocessing
import os
import queue
import time
from builtins import *  # noqa: F401,F403

from future import standard_library
standard_library.install_aliases()

from multiscanner import multiscan, parse_reports
from multiscanner.config import get_config_path, read_config
from multiscanner.storage import storage


__author__ = "Drew Bonasera"
__license__ = "MPL 2.0"

CONFIG = os.path.join(os.path.dirname(__file__), 'distconf.ini')

logger = logging.getLogger(__name__)


def multiscanner_process(work_queue, config, batch_size, wait_seconds, delete, exit_signal):
    filelist = []
    time_stamp = None
    storage_conf = get_config_path('storage', config)
    storage_handler = storage.StorageHandler(configfile=storage_conf)
    while not exit_signal.value:
        time.sleep(1)
        try:
            filelist.append(work_queue.get_nowait())
            if not time_stamp:
                time_stamp = time.time()
            while len(filelist) < batch_size:
                filelist.append(work_queue.get_nowait())
        except queue.Empty:
            if filelist and time_stamp:
                if len(filelist) >= batch_size:
                    pass
                elif time.time() - time_stamp > wait_seconds:
                    pass
                else:
                    continue
            else:
                continue

        resultlist = multiscan(filelist, configfile=config)
        results = parse_reports(resultlist, python=True)
        if delete:
            for file_name in results:
                os.remove(file_name)

        storage_handler.store(results, wait=False)
        logger.info('Scanned {} files'.format(len(results)))

        filelist = []
        time_stamp = None
    storage_handler.close()


def _main():
    args = _parse_args()
    # Pull config options
    conf = read_config(args.config)
    multiscanner_config = conf['worker']['multiscanner_config']

    # Start worker task
    work_queue = multiprocessing.Queue()
    exit_signal = multiprocessing.Value('b')
    exit_signal.value = False
    ms_process = multiprocessing.Process(
            target=multiscanner_process,
            args=(work_queue, multiscanner_config, args.delete, exit_signal))
    ms_process.start()

    # Start message pickup task
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        exit_signal.value = True

    logger.info("Waiting for MultiScanner to exit...")
    ms_process.join()


def _parse_args():
    parser = argparse.ArgumentParser(description='Run MultiScanner tasks via celery')
    parser.add_argument("-c", "--config", help="The config file to use", required=False, default=CONFIG)
    parser.add_argument("--delete", action="store_true", help="Delete files once scanned")
    return parser.parse_args()


if __name__ == '__main__':
    _main()
