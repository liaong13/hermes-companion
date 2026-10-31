"""Memory tests with temporary data and a hard socket deny; no model backend."""
import importlib.util
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('local_life_test', ROOT/'__init__.py',
                                             submodule_search_locations=[str(ROOT)])
life = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = life
spec.loader.exec_module(life)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('memory must not use sockets, including loopback APIs')
    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


@pytest.fixture
def service(tmp_path):
    config = dict(enabled=True, instance_id='demo', domain_id='local-people', policy_revision=1,
                  groups=['20001', '20002'], conversation={'enabled': True}, candidates={'enabled': True})
    service = life.service.LifeMemoryService(tmp_path, tmp_path/'data', config, lambda: config,
                                            channel_domain='social-example', channel_revision=1)
    yield service
    service.close()


def route(mid='1', user='77777', group='20001'):
    return SimpleNamespace(platform='napcat', chat_type='group', chat_id='group:'+group,
        user_id=user, message_id=mid, domain_id='social-example', policy_revision=1, thread_id='')


@pytest.fixture
def relations_service(tmp_path):
    config = dict(enabled=True, instance_id='demo', domain_id='local-people', policy_revision=1,
                  groups=['20001','20002'], conversation={'enabled':True},
                  episodes={'enabled':True}, agreements={'enabled':True,'proactive':True})
    service = life.service.LifeMemoryService(tmp_path,tmp_path/'data',config,lambda:config,
                                            channel_domain='social-example',channel_revision=1)
    yield service
    service.close()


sys.modules['local_life_test_helpers'] = SimpleNamespace(route=route)
