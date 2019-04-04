# Copyright 2019 Apex.AI, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import threading
import unittest

import launch
from launch import LaunchDescription
from launch import LaunchService
from launch.actions import RegisterEventHandler
from launch.event_handlers import OnProcessExit
from launch.event_handlers import OnProcessIO

from .io_handler import ActiveIoHandler
from .parse_arguments import parse_launch_arguments
from .proc_info_handler import ActiveProcInfoHandler
from .test_result import FailResult, TestResult


class _RunnerWorker():

    def __init__(self,
                 test_run,
                 launch_file_arguments=[],
                 debug=False):
        self._test_run = test_run
        self._launch_service = LaunchService(debug=debug)
        self._processes_launched = threading.Event()  # To signal when all processes started
        self._tests_completed = threading.Event()  # To signal when all the tests have finished
        self._launch_file_arguments = launch_file_arguments

        # Can't run LaunchService.run on another thread :-(
        # See https://github.com/ros2/launch/issues/126
        #
        # It would be simpler if we could run the pre-shutdown test and the post-shutdown tests on
        # one thread, and run the launch on another thead.
        #
        # Instead, we'll run the pre-shutdown tests on a background thread concurrent with the
        # launch on the main thread.  Once the launch is stopped, we'll run the post-shutdown
        # tests on the main thread
        self._test_tr = threading.Thread(
            target=self._run_test,
            name='test_runner_thread',
            daemon=True
        )

    def run(self):
        """
        Launch the processes under test and run the tests.

        :return: A tuple of two unittest.Results - one for tests that ran while processes were
        active, and another set for tests that ran after processes were shutdown
        """
        test_ld, test_context = self._test_run.normalized_test_description(
            lambda: self._processes_launched.set()
        )

        # Data that needs to be bound to the tests:
        proc_info = ActiveProcInfoHandler()
        proc_output = ActiveIoHandler()
        test_context = test_context
        # TODO pete: this can be simplified as a call to the dict ctor:
        parsed_launch_arguments = parse_launch_arguments(self._launch_file_arguments)
        test_args = {}
        for k, v in parsed_launch_arguments:
            test_args[k] = v

        self._test_run.bind(
            self._test_run.pre_shutdown_tests,
            injected_attributes={
                'proc_info': proc_info,
                'proc_output': proc_output,
                'test_args': test_args,
            },
            injected_args=dict(
                test_context,
                # Add a few more things to the args dictionary:
                **{
                    'proc_info': proc_info,
                    'proc_output': proc_output,
                    'test_args': test_args
                }
            )
        )
        self._test_run.bind(
            self._test_run.post_shutdown_tests,
            injected_attributes={
                'proc_info': proc_info._proc_info_handler,
                'proc_output': proc_output._io_handler,
                'test_args': test_args,
            },
            injected_args=dict(
                test_context,
                # Add a few more things to the args dictionary:
                **{
                    'proc_info': proc_info._proc_info_handler,
                    'proc_output': proc_output._io_handler,
                    'test_args': test_args
                }
            )
        )

        # Wrap the test_ld in another launch description so we can bind command line arguments to
        # the test and add our own event handlers for process IO and process exit:
        launch_description = LaunchDescription([
            launch.actions.IncludeLaunchDescription(
                launch.LaunchDescriptionSource(launch_description=test_ld),
                launch_arguments=parsed_launch_arguments
            ),
            RegisterEventHandler(
                OnProcessExit(on_exit=lambda info, unused: proc_info.append(info))
            ),
            RegisterEventHandler(
                OnProcessIO(
                    on_stdout=proc_output.append,
                    on_stderr=proc_output.append,
                )
            ),
        ])

        self._launch_service.include_launch_description(
            launch_description
        )

        self._test_tr.start()  # Run the tests on another thread
        self._launch_service.run()  # This will block until the test thread stops it

        if not self._tests_completed.wait(timeout=0):
            # LaunchService.run returned before the tests completed.  This can be because the user
            # did ctrl+c, or because all of the launched nodes died before the tests completed
            print('Processes under test stopped before tests completed')
            # Give some extra help debugging why processes died early
            self._print_process_output_summary(proc_info, proc_output)
            # We treat this as a test failure and return some test results indicating such
            return FailResult(), FailResult()

        inactive_results = unittest.TextTestRunner(
            verbosity=2,
            resultclass=TestResult
        ).run(self._test_run.post_shutdown_tests)

        return self._results, inactive_results

    def _run_test(self):
        # Waits for the DUT processes to start (signaled by the _processes_launched
        # event) and then runs the tests

        if not self._processes_launched.wait(timeout=15):
            # Timed out waiting for the processes to start
            print('Timed out waiting for processes to start up')
            self._launch_service.shutdown()
            return

        try:
            # Run the tests
            self._results = unittest.TextTestRunner(
                verbosity=2,
                resultclass=TestResult
            ).run(self._test_run.pre_shutdown_tests)

        finally:
            self._tests_completed.set()
            self._launch_service.shutdown()

    def _print_process_output_summary(self, proc_info, proc_output):
        failed_procs = [proc for proc in proc_info if proc.returncode != 0]

        for process in failed_procs:
            print("Process '{}' exited with {}".format(process.process_name, process.returncode))
            print("##### '{}' output #####".format(process.process_name))
            try:
                for io in proc_output[process.action]:
                    print('{}'.format(io.text.decode('ascii')))
            except KeyError:
                pass  # Process generated no output
            print('#' * (len(process.process_name) + 21))


class ApexRunner(object):

    def __init__(self,
                 test_runs,
                 launch_file_arguments=[],
                 debug=False):
        """
        Create an ApexRunner object.

        :param callable gen_launch_description_fn: A function that returns a ros2 LaunchDesription
        for launching the processes under test.  This function should take a callable as a
        parameter which will be called when the processes under test are ready for the test to
        start
        """
        self._test_runs = test_runs
        self._launch_file_arguments = launch_file_arguments
        self._debug = debug

    def run(self):
        """
        Launch the processes under test and run the tests.

        :return: A tuple of two unittest.Results - one for tests that ran while processes were
        active, and another set for tests that ran after processes were shutdown
        """
        for run in self._test_runs:
            worker = _RunnerWorker(run, self._launch_file_arguments, self._debug)
            # TODO pete: Make this work for parameterized launches by combining the results
            return worker.run()

    def validate(self):
        """Inspect the test configuration for configuration errors."""
        # Make sure the function signature of the launch configuration
        # generator is correct
        for run in self._test_runs:
            inspect.getcallargs(run.test_description_function, lambda: None)
