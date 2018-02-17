# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

import os
import sys
import select

# Note: This work-around is required to fix the issue with other Python modules which live
# inside this directory polluting and masking sys.path for Python runner actions.
# Since this module is ran as a Python script inside a subprocess, directory where the script
# lives gets added to sys.path and we don't want that.
# Note: We need to use just the suffix, because full path is different depending if the process
# is ran in virtualenv or not
RUNNERS_PATH_SUFFIX = 'st2common/runners'
if __name__ == '__main__':
    script_path = sys.path[0]
    if RUNNERS_PATH_SUFFIX in script_path:
        sys.path.pop(0)

import sys
import json
import argparse
import traceback

from st2common import log as logging
from st2common import config as st2common_config
from st2common.runners.base_action import Action
from st2common.runners.utils import get_logger_for_python_runner_action
from st2common.runners.utils import get_action_class_instance
from st2common.util import loader as action_loader
from st2common.constants.action import ACTION_OUTPUT_RESULT_DELIMITER
from st2common.constants.keyvalue import SYSTEM_SCOPE
from st2common.constants.runners import PYTHON_RUNNER_INVALID_ACTION_STATUS_EXIT_CODE
from st2common.constants.runners import PYTHON_RUNNER_DEFAULT_LOG_LEVEL

__all__ = [
    'PythonActionWrapper',
    'ActionService'
]

LOG = logging.getLogger(__name__)

INVALID_STATUS_ERROR_MESSAGE = """
If this is an existing action which returns a tuple with two items, it needs to be updated to
either:

1. Return a list instead of a tuple
2. Return a tuple where a first items is a status flag - (True, ('item1', 'item2'))

For more information, please see: https://docs.stackstorm.com/upgrade_notes.html#st2-v1-6
""".strip()

# How many seconds to wait for stdin input when parameters are passed in via stdin before
# timing out
READ_STDIN_INPUT_TIMEOUT = 2


class ActionService(object):
    """
    Instance of this class is passed to the action instance and exposes "public" methods which can
    be called by the action.
    """

    def __init__(self, action_wrapper):
        self._action_wrapper = action_wrapper
        self._datastore_service = None

    @property
    def datastore_service(self):
        # Late import to avoid very expensive in-direct import (~1 second) when this function is
        # not called / used
        from st2common.services.datastore import ActionDatastoreService

        if not self._datastore_service:
            # Note: We use temporary auth token generated by the container which is valid for the
            # duration of the action lifetime
            action_name = self._action_wrapper._class_name
            log_level = self._action_wrapper._log_level
            logger = get_logger_for_python_runner_action(action_name=action_name,
                                                         log_level=log_level)
            pack_name = self._action_wrapper._pack
            class_name = self._action_wrapper._class_name
            auth_token = os.environ.get('ST2_ACTION_AUTH_TOKEN', None)
            self._datastore_service = ActionDatastoreService(logger=logger,
                                                             pack_name=pack_name,
                                                             class_name=class_name,
                                                             auth_token=auth_token)
        return self._datastore_service

    ##################################
    # General methods
    ##################################

    def get_user_info(self):
        return self.datastore_service.get_user_info()

    ##################################
    # Methods for datastore management
    ##################################

    def list_values(self, local=True, prefix=None):
        return self.datastore_service.list_values(local, prefix)

    def get_value(self, name, local=True, scope=SYSTEM_SCOPE, decrypt=False):
        return self.datastore_service.get_value(name, local, scope=scope, decrypt=decrypt)

    def set_value(self, name, value, ttl=None, local=True, scope=SYSTEM_SCOPE, encrypt=False):
        return self.datastore_service.set_value(name, value, ttl, local, scope=scope,
                                                encrypt=encrypt)

    def delete_value(self, name, local=True, scope=SYSTEM_SCOPE):
        return self.datastore_service.delete_value(name, local)


class PythonActionWrapper(object):
    def __init__(self, pack, file_path, config=None, parameters=None, user=None, parent_args=None,
                 log_level=PYTHON_RUNNER_DEFAULT_LOG_LEVEL):
        """
        :param pack: Name of the pack this action belongs to.
        :type pack: ``str``

        :param file_path: Path to the action module.
        :type file_path: ``str``

        :param config: Pack config.
        :type config: ``dict``

        :param parameters: action parameters.
        :type parameters: ``dict`` or ``None``

        :param user: Name of the user who triggered this action execution.
        :type user: ``str``

        :param parent_args: Command line arguments passed to the parent process.
        :type parse_args: ``list``
        """

        self._pack = pack
        self._file_path = file_path
        self._config = config or {}
        self._parameters = parameters or {}
        self._user = user
        self._parent_args = parent_args or []
        self._log_level = log_level

        self._class_name = None
        self._logger = logging.getLogger('PythonActionWrapper')

        try:
            st2common_config.parse_args(args=self._parent_args)
        except Exception as e:
            LOG.debug('Failed to parse config using parent args (parent_args=%s): %s' %
                      (str(self._parent_args), str(e)))

        # Note: We can only set a default user value if one is not provided after parsing the
        # config
        if not self._user:
            # Note: We use late import to avoid performance overhead
            from oslo_config import cfg
            self._user = cfg.CONF.system_user.user

    def run(self):
        action = self._get_action_instance()
        output = action.run(**self._parameters)

        if isinstance(output, tuple) and len(output) == 2:
            # run() method returned status and data - (status, data)
            action_status = output[0]
            action_result = output[1]
        else:
            # run() method returned only data, no status (pre StackStorm v1.6)
            action_status = None
            action_result = output

        action_output = {
            'result': action_result,
            'status': None
        }

        if action_status is not None and not isinstance(action_status, bool):
            sys.stderr.write('Status returned from the action run() method must either be '
                             'True or False, got: %s\n' % (action_status))
            sys.stderr.write(INVALID_STATUS_ERROR_MESSAGE)
            sys.exit(PYTHON_RUNNER_INVALID_ACTION_STATUS_EXIT_CODE)

        if action_status is not None and isinstance(action_status, bool):
            action_output['status'] = action_status

            # Special case if result object is not JSON serializable - aka user wanted to return a
            # non-simple type (e.g. class instance or other non-JSON serializable type)
            try:
                json.dumps(action_output['result'])
            except TypeError:
                action_output['result'] = str(action_output['result'])

        try:
            print_output = json.dumps(action_output)
        except Exception:
            print_output = str(action_output)

        # Print output to stdout so the parent can capture it
        sys.stdout.write(ACTION_OUTPUT_RESULT_DELIMITER)
        sys.stdout.write(print_output + '\n')
        sys.stdout.write(ACTION_OUTPUT_RESULT_DELIMITER)
        sys.stdout.flush()

    def _get_action_instance(self):
        try:
            actions_cls = action_loader.register_plugin(Action, self._file_path)
        except Exception as e:
            tb_msg = traceback.format_exc()
            msg = ('Failed to load action class from file "%s" (action file most likely doesn\'t '
                   'exist or contains invalid syntax): %s' % (self._file_path, str(e)))
            msg += '\n\n' + tb_msg
            exc_cls = type(e)
            raise exc_cls(msg)

        action_cls = actions_cls[0] if actions_cls and len(actions_cls) > 0 else None

        if not action_cls:
            raise Exception('File "%s" has no action class or the file doesn\'t exist.' %
                            (self._file_path))

        # Retrieve name of the action class
        # Note - we need to either use cls.__name_ or inspect.getmro(cls)[0].__name__ to
        # retrieve a correct name
        self._class_name = action_cls.__name__

        action_service = ActionService(action_wrapper=self)
        action_instance = get_action_class_instance(action_cls=action_cls,
                                                    config=self._config,
                                                    action_service=action_service)
        return action_instance


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Python action runner process wrapper')
    parser.add_argument('--pack', required=True,
                        help='Name of the pack this action belongs to')
    parser.add_argument('--file-path', required=True,
                        help='Path to the action module')
    parser.add_argument('--config', required=False,
                        help='Pack config serialized as JSON')
    parser.add_argument('--parameters', required=False,
                        help='Serialized action parameters')
    parser.add_argument('--stdin-parameters', required=False, action='store_true',
                        help='Serialized action parameters via stdin')
    parser.add_argument('--user', required=False,
                        help='User who triggered the action execution')
    parser.add_argument('--parent-args', required=False,
                        help='Command line arguments passed to the parent process serialized as '
                             ' JSON')
    parser.add_argument('--log-level', required=False, default=PYTHON_RUNNER_DEFAULT_LOG_LEVEL,
                        help='Log level for actions')
    args = parser.parse_args()

    config = json.loads(args.config) if args.config else {}
    user = args.user
    parent_args = json.loads(args.parent_args) if args.parent_args else []
    log_level = args.log_level

    parameters = {}

    if args.parameters:
        LOG.debug('Getting parameters from argument')
        args_parameters = args.parameters
        args_parameters = json.loads(args_parameters) if args_parameters else {}
        parameters.update(args_parameters)

    if args.stdin_parameters:
        LOG.debug('Getting parameters from stdin')

        i, _, _ = select.select([sys.stdin], [], [], READ_STDIN_INPUT_TIMEOUT)

        if not i:
            raise ValueError(('No input received and timed out while waiting for '
                              'parameters from stdin'))

        stdin_parameters = json.loads(sys.stdin.readline().strip())
        stdin_parameters = stdin_parameters.get('parameters', {})
        parameters.update(stdin_parameters)

    LOG.debug('Received parameters: %s', parameters)

    assert isinstance(parent_args, list)
    obj = PythonActionWrapper(pack=args.pack,
                              file_path=args.file_path,
                              config=config,
                              parameters=parameters,
                              user=user,
                              parent_args=parent_args,
                              log_level=log_level)

    obj.run()
