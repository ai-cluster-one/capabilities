"""C2 regression tests replay anonymized Coolify 4.3.23 API responses."""
import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

BUNDLE = Path(__file__).resolve().parents[1]
m = types.ModuleType('coolify_deploy_loop')
m.__file__ = str(BUNDLE / 'bin' / 'coolify')
exec(Path(m.__file__).read_text(), m.__dict__)
FIXTURES = BUNDLE / 'tests' / 'fixtures' / 'c2'


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def client(responses):
    calls = []
    def handle(req):
        calls.append(req)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)
    return httpx.Client(base_url='https://example.test/api/v1',
                        transport=httpx.MockTransport(handle)), calls


def invoke(argv, c, writable=True):
    with patch.object(sys, 'argv', ['coolify', *argv]), patch.object(m, '_gate'), patch.object(m, '_contract'), patch.object(m, '_resolve_conn', return_value={'id':'fixture','allow_write':writable}), patch.object(m, '_client', return_value=c):
        m.main()


@pytest.mark.parametrize('verb,base,response', [
    ('app','applications','app-delete'), ('service','services','service-delete'),
    ('database','databases','database-delete'), ('projects','projects','project-delete')])
def test_delete_cli_defaults_preserve_volumes(verb, base, response, capsys):
    c, calls = client([(200,fixture(response))])
    invoke([verb,'delete','resource-fixture','--yes'],c)
    assert len(calls)==1
    assert calls[0].method=='DELETE'
    assert calls[0].url.path==f'/api/v1/{base}/resource-fixture'
    expected={} if verb=='projects' else {k:'false' for k in m.DELETE_FLAGS}
    assert dict(calls[0].url.params)==expected
    assert json.loads(capsys.readouterr().out)==fixture(response)


@pytest.mark.parametrize('verb', ['app','service','database','projects'])
@pytest.mark.parametrize('writable,yes,exit_code', [(True,False,6),(False,True,4)])
def test_delete_refuses_before_http(verb,writable,yes,exit_code):
    c, calls=client([])
    with pytest.raises(SystemExit) as err:
        invoke([verb,'delete','resource-fixture',*(['--yes'] if yes else [])],c,writable)
    assert err.value.code==exit_code
    assert not calls


@pytest.mark.parametrize('verb', ['app','service','database'])
@pytest.mark.parametrize('flag', m.DELETE_FLAGS)
def test_cleanup_is_individually_opted_in(verb,flag):
    c,calls=client([(200,fixture('service-delete'))])
    invoke([verb,'delete','resource-fixture','--yes','--'+flag.replace('_','-')],c)
    assert dict(calls[0].url.params)=={k:str(k==flag).lower() for k in m.DELETE_FLAGS}


def test_project_cleanup_flags_refused():
    c,calls=client([])
    with pytest.raises(SystemExit) as err:
        invoke(['projects','delete','project-fixture','--yes','--delete-volumes'],c)
    assert err.value.code==6 and not calls


def test_projects_create_keeps_c1_argument_form(capsys):
    c,calls=client([(200,fixture('project-create'))])
    invoke(['projects','create','fixture project'],c)
    assert json.loads(calls[0].content)=={'name':'fixture project'}
    assert json.loads(capsys.readouterr().out)==fixture('project-create')


@pytest.mark.parametrize('response',['app-deploy','service-deploy'])
def test_deploy_preserves_api_identifiers(response,capsys):
    c,calls=client([(200,fixture(response))])
    invoke(['deploy','resource-fixture'],c)
    assert json.loads(capsys.readouterr().out)==fixture(response)
    assert calls[0].method=='POST'


def test_deploy_tag_needs_no_resource_uuid():
    c,calls=client([(200,fixture('app-deploy'))])
    invoke(['deploy','--tag','fixture-tag'],c)
    assert dict(calls[0].url.params)=={'tag':'fixture-tag'}
    assert 'every resource with a tag' in m.__doc__
    assert 'by image tag instead' not in m.__doc__


def test_app_instant_deploy_queues_once_and_returns_uuid(capsys):
    c,calls=client([(200,fixture('app-create')),(200,fixture('app-deploy'))])
    invoke(['app','create','--image','nginx:alpine','--project','project-fixture','--server','server-fixture','--environment','production','--instant-deploy'],c)
    assert len(calls)==2
    assert json.loads(calls[0].content)['instant_deploy'] is False
    assert calls[1].url.path=='/api/v1/deploy'
    result=json.loads(capsys.readouterr().out)
    assert result['uuid']=='app-fixture'
    assert result['deployment_uuid']=='deployment-fixture'


def test_deployments_list_keeps_identifiers():
    c,_=client([(200,fixture('deployments-list'))])
    result=m.cmd_collection(c,'deployments',None)
    row=fixture('deployments-list')[0]
    assert result[0]['deployment_uuid']==row['deployment_uuid']
    assert result[0]['application_id']==row['application_id']
    for field in ('resource_uuid','application_uuid'):
        assert m._slim(dict(row,**{field:'resource-fixture'}))[field]=='resource-fixture'


def test_422_keeps_field_names_and_messages(capsys):
    response=fixture('validation-422')
    c,_=client([(422,response)])
    with pytest.raises(SystemExit) as err:
        m._request(c,'POST','/applications/dockerimage',json_body={'name':'fixture'})
    assert err.value.code==6
    message=json.loads(capsys.readouterr().err)['error']['message']
    for field,messages in response['errors'].items():
        assert field in message
        assert all(v in message for v in messages)


def test_422_redacts_password_field(capsys):
    c,_=client([(422,{'message':'Validation failed.','errors':{'password':['secret-value']}})])
    with pytest.raises(SystemExit):m._request(c,'POST','/applications/dockerimage')
    assert 'secret-value' not in capsys.readouterr().err


@pytest.mark.parametrize('with_fields', [True, False])
def test_422_redacts_message_field_names_and_errors(with_fields, capsys):
    response = fixture('validation-422-secrets')
    if not with_fields:
        response.pop('errors')
    c, _ = client([(422, response)])
    with pytest.raises(SystemExit) as err:
        m._request(c, 'POST', '/applications/dockerimage')
    assert err.value.code == 6
    captured = capsys.readouterr()
    assert captured.out == ''
    for secret in ('fixture-message-secret', 'fixture-field-secret',
                   'fixture-name-secret', 'fixture-error-secret'):
        assert secret not in captured.out
        assert secret not in captured.err
    message = json.loads(captured.err)['error']['message']
    assert 'postgres://app:<redacted>@db.example.test:5432/app' in message
    if with_fields:
        assert 'postgres_password: <redacted>' in message
        assert 'Invalid database URL.' in message


def states(status,uuid='app-fixture'):
    return [{'uuid':uuid,'status':status}]


def test_wait_rides_out_exited_and_stale_health():
    finished=fixture('deployment-get')
    queued=dict(finished,status='queued')
    c,calls=client([(200,queued),(200,states('running:healthy')),
                    (200,dict(finished,status='in_progress')),(200,states('exited:unhealthy')),
                    (200,finished),(200,states('exited:unhealthy')),
                    (200,finished),(200,states('running:healthy'))])
    with patch.object(m.time,'sleep'):
        result=m.cmd_wait(c,'app-fixture','deployment-fixture',600)
    assert result==fixture('app-wait')
    assert len(calls)==8


def test_wait_service_without_deployment_on_read_only_connection(capsys):
    c,_=client([(200,states('running:healthy','service-fixture'))])
    invoke(['wait','service-fixture','--timeout','1'],c,False)
    assert json.loads(capsys.readouterr().out)==fixture('service-wait')


@pytest.mark.parametrize('status',['failed','cancelled','canceled','cancelled-by-user'])
def test_wait_failed_deployment(status,capsys):
    c,calls=client([(200,dict(fixture('deployment-get'),status=status))])
    with pytest.raises(SystemExit) as err:m.cmd_wait(c,'app-fixture','deployment-fixture',600)
    assert err.value.code==5 and len(calls)==1
    assert status in capsys.readouterr().err


def test_wait_timeout_carries_last_states(capsys):
    c,_=client([(200,fixture('deployment-get')),(200,states('exited:unhealthy'))])
    with patch.object(m.time,'monotonic',side_effect=[0,0,0,2,2]), pytest.raises(SystemExit) as err:
        m.cmd_wait(c,'app-fixture','deployment-fixture',1)
    assert err.value.code==5
    error=json.loads(capsys.readouterr().err)['error']
    assert error['code']=='wait_timeout'
    last=json.loads(error['message'])
    assert last['status']=='exited:unhealthy' and last['deployment_status']=='finished'


@pytest.mark.parametrize('timeout',[0,-1,float('inf'),float('nan')])
def test_wait_bad_timeout_never_requests(timeout):
    c,calls=client([])
    with pytest.raises(SystemExit) as err:m.cmd_wait(c,'app-fixture',None,timeout)
    assert err.value.code==6 and not calls


def test_wait_missing_resource():
    c,_=client([(200,[])])
    with pytest.raises(SystemExit) as err:m.cmd_wait(c,'missing',None,600)
    assert err.value.code==3


def test_wait_http_deadline_is_bounded_and_reports_timeout(capsys):
    clock = [0.0]
    def handle(req):
        assert all(value <= 1 for value in req.extensions['timeout'].values())
        clock[0] = 2.0
        raise httpx.ReadTimeout('late', request=req)
    c = httpx.Client(base_url='https://example.test/api/v1', transport=httpx.MockTransport(handle))
    with patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), pytest.raises(SystemExit) as err:
        m.cmd_wait(c, 'app-fixture', None, 1)
    assert err.value.code == 5
    assert json.loads(capsys.readouterr().err)['error']['code'] == 'wait_timeout'
