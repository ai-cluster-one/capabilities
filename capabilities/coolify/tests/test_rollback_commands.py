"""C3 `app rollback` and `app rollback-images` replay Coolify 4.3.23 answers."""
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

BUNDLE = Path(__file__).resolve().parents[1]
m = types.ModuleType('coolify_rollback')
m.__file__ = str(BUNDLE / 'bin' / 'coolify')
exec(Path(m.__file__).read_text(), m.__dict__)
FIXTURES = BUNDLE / 'tests' / 'fixtures' / 'c3'
TOKEN = 'fixture|token-never-printed'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def client(responses):
    calls = []

    def handle(req):
        calls.append(req)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.Client(base_url='https://example.test/api/v1',
                        headers={'Authorization': 'Bearer ' + TOKEN},
                        transport=httpx.MockTransport(handle)), calls


def invoke(argv, c, writable=True):
    with patch.object(sys, 'argv', ['coolify', *argv]), patch.object(m, '_gate'), \
            patch.object(m, '_contract'), \
            patch.object(m, '_resolve_conn', return_value={'id': 'fixture', 'allow_write': writable}), \
            patch.object(m, '_client', return_value=c):
        m.main()


def test_rollback_posts_the_ref_and_returns_the_deployment_to_wait_on(capsys):
    c, calls = client([(200, fixture('app-rollback'))])
    invoke(['app', 'rollback', 'application-fixture', '--to', 'release/1.2'], c)
    assert len(calls) == 1
    assert calls[0].method == 'POST'
    assert calls[0].url.path == '/api/v1/applications/application-fixture/rollback'
    assert json.loads(calls[0].content) == {'commit': 'release/1.2'}
    out = capsys.readouterr()
    assert json.loads(out.out)['deployment_uuid'] == 'rollback-deployment-fixture'
    assert TOKEN not in out.out + out.err


def test_a_skipped_rollback_is_answered_without_a_deployment(capsys):
    c, _calls = client([(200, fixture('app-rollback-skipped'))])
    invoke(['app', 'rollback', 'application-fixture', '--to', 'a1b2c3d'], c)
    assert 'deployment_uuid' not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('ref', ['-flag', 'a b', 'ref;rm', '', '../x'])
def test_a_ref_coolify_would_refuse_is_refused_before_http(ref):
    c, calls = client([])
    with pytest.raises(SystemExit) as err:
        invoke(['app', 'rollback', 'application-fixture', '--to=' + ref], c)
    assert err.value.code == 6 and not calls


def test_rollback_is_a_write_and_a_read_only_connection_refuses_it():
    c, calls = client([])
    with pytest.raises(SystemExit) as err:
        invoke(['app', 'rollback', 'application-fixture', '--to', 'a1b2c3d'], c, writable=False)
    assert err.value.code == 4 and not calls


def test_rollback_requires_a_target():
    c, calls = client([])
    with pytest.raises(SystemExit) as err:
        invoke(['app', 'rollback', 'application-fixture'], c)
    assert err.value.code == 2 and not calls


def test_coolify_refusing_the_ref_is_reported_with_its_field(capsys):
    c, _calls = client([(422, fixture('app-rollback-422'))])
    with pytest.raises(SystemExit) as err:
        invoke(['app', 'rollback', 'application-fixture', '--to', 'a1b2c3d'], c)
    assert err.value.code == 6
    assert 'commit' in capsys.readouterr().err


@pytest.mark.parametrize('name', ['app-rollback-images', 'app-rollback-images-compose'])
def test_rollback_images_reads_what_a_rollback_can_return_to(capsys, name):
    c, calls = client([(200, fixture(name))])
    # A read: a connection that may not write can still list them.
    invoke(['app', 'rollback-images', 'application-fixture'], c, writable=False)
    assert calls[0].method == 'GET'
    assert calls[0].url.path == '/api/v1/applications/application-fixture/rollback-images'
    assert json.loads(capsys.readouterr().out) == fixture(name)


def test_the_help_names_both_verbs_where_deployment_looks_for_them():
    help_text = m.__doc__ or ''
    assert 'app rollback <uuid> --to <commit|image-tag>' in help_text
    assert 'app rollback-images <uuid>' in help_text
