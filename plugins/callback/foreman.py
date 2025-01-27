# -*- coding: utf-8 -*-
# (c) 2015, 2016 Daniel Lobato <elobatocs@gmail.com>
# (c) 2016 Guido Günther <agx@sigxcpu.org>
# (c) 2017 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

# pylint: disable=super-with-arguments

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = '''
    callback: theforeman.foreman.foreman
    type: notification
    short_description: Sends events to Foreman
    description:
      - This callback will report facts and task events to Foreman
    requirements:
      - whitelisting in configuration
      - requests (python library)
    options:
      url:
        description:
          - URL of the Foreman server.
        env:
          - name: FOREMAN_URL
          - name: FOREMAN_SERVER_URL
          - name: FOREMAN_SERVER
        required: True
        default: http://localhost:3000
        ini:
          - section: callback_foreman
            key: url
      client_cert:
        description:
          - X509 certificate to authenticate to Foreman if https is used
        env:
            - name: FOREMAN_SSL_CERT
        default: /etc/foreman/client_cert.pem
        ini:
          - section: callback_foreman
            key: ssl_cert
          - section: callback_foreman
            key: client_cert
        aliases: [ ssl_cert ]
      client_key:
        description:
          - the corresponding private key
        env:
          - name: FOREMAN_SSL_KEY
        default: /etc/foreman/client_key.pem
        ini:
          - section: callback_foreman
            key: ssl_key
          - section: callback_foreman
            key: client_key
        aliases: [ ssl_key ]
      verify_certs:
        description:
          - Toggle to decide whether to verify the Foreman certificate.
          - It can be set to '1' to verify SSL certificates using the installed CAs or to a path pointing to a CA bundle.
          - Set to '0' to disable certificate checking.
        env:
          - name: FOREMAN_SSL_VERIFY
        default: 1
        ini:
          - section: callback_foreman
            key: verify_certs
      disable_callback:
        description:
          - Toggle to make the callback plugin disable itself even if it is loaded.
          - It can be set to '1' to prevent the plugin from being used even if it gets loaded.
        env:
          - name: FOREMAN_CALLBACK_DISABLE
        default: 0
'''

import os
from datetime import datetime
from collections import defaultdict
import json
import time

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

from ansible.module_utils._text import to_text
from ansible.module_utils.parsing.convert_bool import boolean as to_bool
from ansible.plugins.callback import CallbackBase


def build_log(data):
    """
    Transform the internal log structure to one accepted by Foreman's
    config_report API.
    """
    for source, msg in data:
        if 'failed' in msg:
            level = 'err'
        elif 'changed' in msg and msg['changed']:
            level = 'notice'
        else:
            level = 'info'

        yield {
            "log": {
                'sources': {
                    'source': source,
                },
                'messages': {
                    'message': json.dumps(msg),
                },
                'level': level,
            }
        }


def get_time():
    """
    Return the time for measuring duration. Prefers monotonic time but
    falls back to the regular time on older Python versions.
    """
    try:
        return time.monotonic()
    except AttributeError:
        return time.time()


def get_now():
    """
    Return the current timestamp as a string to be sent over the network.
    """
    return datetime.utcnow().isoformat()


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'notification'
    CALLBACK_NAME = 'theforeman.foreman.foreman'
    CALLBACK_NEEDS_WHITELIST = True

    def __init__(self):
        super(CallbackModule, self).__init__()
        self.items = defaultdict(list)
        self.facts = defaultdict(dict)
        self.start_time = get_time()

    def set_options(self, task_keys=None, var_options=None, direct=None):

        super(CallbackModule, self).set_options(task_keys=task_keys, var_options=var_options, direct=direct)

        if self.get_option('disable_callback'):
            self._disable_plugin('Callback disabled by environment.')

        self.foreman_url = self.get_option('url')
        ssl_cert = self.get_option('client_cert')
        ssl_key = self.get_option('client_key')

        if HAS_REQUESTS:
            requests_version = tuple(map(int, requests.__version__.split('.')))
            if requests_version < (2, 14):
                self._disable_plugin(u'The `requests` python module is older than 2.14.0.')
        else:
            self._disable_plugin(u'The `requests` python module is not installed')

        if self.foreman_url.startswith('https://'):
            if not os.path.exists(ssl_cert):
                self._disable_plugin(u'FOREMAN_SSL_CERT %s not found.' % ssl_cert)

            if not os.path.exists(ssl_key):
                self._disable_plugin(u'FOREMAN_SSL_KEY %s not found.' % ssl_key)

        self.session = requests.Session()
        self.session.verify = self._ssl_verify(str(self.get_option('verify_certs')))
        self.session.cert = (ssl_cert, ssl_key)

    def _disable_plugin(self, msg):
        self.disabled = True
        if msg:
            self._display.warning(msg + u' Disabling the Foreman callback plugin.')
        else:
            self._display.warning(u'Disabling the Foreman callback plugin.')

    def _ssl_verify(self, option):
        try:
            verify = to_bool(option)
        except TypeError:
            verify = option

        if verify is False:  # is only set to bool if try block succeeds
            requests.packages.urllib3.disable_warnings()
            self._display.warning(
                u"SSL verification of %s disabled" % self.foreman_url,
            )

        return verify

    def send_facts(self):
        """
        Sends facts to Foreman, to be parsed by foreman_ansible fact
        parser.  The default fact importer should import these facts
        properly.
        """
        url = self.foreman_url + '/api/v2/hosts/facts'

        for host, facts in self.facts.items():
            facts = {
                "name": host,
                "facts": {
                    "ansible_facts": self.facts[host],
                    "_type": "ansible",
                    "_timestamp": get_now(),
                },
            }

            try:
                response = self.session.post(url=url, json=facts)
                response.raise_for_status()
            except requests.exceptions.RequestException as err:
                self._display.warning(u'Sending facts to Foreman at {url} failed for {host}: {err}'.format(
                    host=to_text(host), err=to_text(err), url=to_text(self.foreman_url)))

    def send_reports(self, stats):
        """
        Send reports to Foreman to be parsed by its config report
        importer. THe data is in a format that Foreman can handle
        without writing another report importer.
        """
        url = self.foreman_url + '/api/v2/config_reports'

        for host in stats.processed.keys():
            total = stats.summarize(host)
            report = {
                "config_report": {
                    "host": host,
                    "reported_at": get_now(),
                    "metrics": {
                        "time": {
                            "total": int(get_time() - self.start_time)
                        }
                    },
                    "status": {
                        "applied": total['changed'],
                        "failed": total['failures'] + total['unreachable'],
                        "skipped": total['skipped'],
                    },
                    "logs": list(build_log(self.items[host])),
                    "reporter": "ansible",
                }
            }
            try:
                response = self.session.post(url=url, json=report)
                response.raise_for_status()
            except requests.exceptions.RequestException as err:
                self._display.warning(u'Sending report to Foreman at {url} failed for {host}: {err}'.format(
                    host=to_text(host), err=to_text(err), url=to_text(self.foreman_url)))

            self.items[host] = []

    def append_result(self, result):
        name = result._task.get_name()
        host = result._host.get_name()
        value = result._result
        self.items[host].append((name, value))
        if 'ansible_facts' in value:
            self.facts[host].update(value['ansible_facts'])

    # Ansible callback API
    def v2_runner_on_failed(self, result, ignore_errors=False):
        self.append_result(result)

    def v2_runner_on_unreachable(self, result):
        self.append_result(result)

    def v2_runner_on_async_ok(self, result):
        self.append_result(result)

    def v2_runner_on_async_failed(self, result):
        self.append_result(result)

    def v2_playbook_on_stats(self, stats):
        self.send_facts()
        self.send_reports(stats)

    def v2_runner_on_ok(self, result):
        self.append_result(result)
