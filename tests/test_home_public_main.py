"""Fixed public main proof and content-addressed local mirror reuse; no network."""
import importlib
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

API = 'https://api.github.com/repos/zdaiwmm/lead/git/ref/heads/main'
HEAD = 'b' * 40


@pytest.fixture
def release(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    return importlib.import_module('home_systemd_release')


@pytest.fixture
def http(monkeypatch):
    state = SimpleNamespace(body=json.dumps({'ref': 'refs/heads/main',
        'object': {'type': 'commit', 'sha': HEAD}}).encode(), status=200, calls=[], handlers=[], error=None)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @property
        def status(self):
            return state.status

        def read(self, limit):
            assert limit == 65537
            return state.body[:limit]

    def build(*handlers):
        state.handlers.extend(handlers)

        def open(request, timeout):
            state.calls.append((request, timeout))
            if state.error:
                raise state.error
            return Response()

        return SimpleNamespace(open=open)

    monkeypatch.setattr(urllib.request, 'build_opener', build)
    return state


def test_public_main_is_fixed_bounded_direct_tls_without_credentials(release, http, monkeypatch):
    assert callable(getattr(release, 'public_main', None)), 'public main proof is missing'
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted.invalid:8888')
    monkeypatch.setenv('GH_TOKEN', 'synthetic-must-not-be-forwarded')
    assert release.public_main() == HEAD
    request, timeout = http.calls[0]
    assert request.full_url == API and timeout == 20
    assert not request.has_header('Authorization')
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in http.handlers)
    redirects = [h for h in http.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert redirects[0].redirect_request(request, None, 302, '', {}, 'https://untrusted.invalid') is None


@pytest.mark.parametrize('body', [b'null', b'[]', b'{}', b'not-json', b' ' * 65537,
    json.dumps({'ref': 'refs/heads/other', 'object': {'type': 'commit', 'sha': HEAD}}).encode(),
    json.dumps({'ref': 'refs/heads/main', 'object': {'type': 'tag', 'sha': HEAD}}).encode(),
    json.dumps({'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'B' * 40}}).encode(),
    json.dumps({'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': HEAD[:-1]}}).encode(),
    json.dumps({'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 7}}).encode(),
    b'{"ref":"refs/heads/other","ref":"refs/heads/main","object":{"type":"commit","sha":"' + HEAD.encode() + b'"}}'])
def test_untrusted_public_response_never_becomes_main(release, http, body):
    http.body = body
    with pytest.raises(release.Refused):
        release.public_main()


@pytest.mark.parametrize('status', [201, 301, 302, 403, 500])
def test_non_success_status_is_refused(release, http, status):
    http.status = status
    with pytest.raises(release.Refused):
        release.public_main()


def test_public_main_unavailable_does_not_fall_back_to_local_or_fetch(release, http, monkeypatch):
    http.error = urllib.error.URLError('synthetic unavailable')
    calls = []
    monkeypatch.setattr(release, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(release.Refused):
        release.sync_public_main(Path('/synthetic/lead.git'), 'a' * 40)
    assert calls == []


@pytest.fixture
def mirror(release, monkeypatch):
    state = SimpleNamespace(local=HEAD, published=[HEAD, HEAD], calls=[], fetch_error=False,
        fetched=HEAD, api_calls=0)

    def public():
        state.api_calls += 1
        return state.published.pop(0)

    def run(*args, **kwargs):
        state.calls.append(args)
        if 'rev-parse' in args:
            if state.local is None:
                raise subprocess.CalledProcessError(1, args)
            return state.local
        if 'fetch' in args:
            if state.fetch_error:
                raise subprocess.CalledProcessError(128, args)
            state.local = state.fetched
        return ''

    monkeypatch.setattr(release, 'public_main', public)
    monkeypatch.setattr(release, 'run', run)
    return state


def test_precise_imported_main_reuses_objects_and_keeps_ancestry(release, mirror):
    assert release.sync_public_main(Path('/synthetic/lead.git'), 'a' * 40) == HEAD
    assert mirror.api_calls == 2 and not any('fetch' in call for call in mirror.calls)
    assert mirror.calls[-1][-4:] == ('merge-base', '--is-ancestor', 'a' * 40, 'refs/heads/main')


@pytest.mark.parametrize('local', [None, 'c' * 40])
def test_missing_or_different_main_requires_the_original_fixed_fetch(release, mirror, local):
    mirror.local = local
    assert release.sync_public_main(Path('/synthetic/lead.git'), 'a' * 40) == HEAD
    fetch = next(call for call in mirror.calls if 'fetch' in call)
    assert fetch[-4:] == ('fetch', '--no-tags', 'https://github.com/zdaiwmm/lead.git',
                          '+refs/heads/main:refs/heads/main')
    assert 'credential.helper=' in fetch and 'core.hooksPath=/dev/null' in fetch
    assert mirror.api_calls == 2


@pytest.mark.parametrize('changed', ['public', 'local'])
def test_changed_main_fails_before_any_ancestry_admission(release, mirror, changed):
    if changed == 'public':
        mirror.published = [HEAD, 'c' * 40]
    else:
        mirror.local = 'c' * 40
        mirror.fetched = 'd' * 40
    with pytest.raises(release.Refused, match='main'):
        release.sync_public_main(Path('/synthetic/lead.git'), 'a' * 40)
    assert not any('merge-base' in call for call in mirror.calls)


def test_failed_required_fetch_never_falls_back_to_stale_main(release, mirror):
    mirror.local = 'c' * 40
    mirror.fetch_error = True
    with pytest.raises(subprocess.CalledProcessError):
        release.sync_public_main(Path('/synthetic/lead.git'), 'a' * 40)
    assert mirror.api_calls == 1 and not any('merge-base' in call for call in mirror.calls)


def test_reuse_checks_actual_git_objects_and_rejects_unrelated_candidate(release, monkeypatch, tmp_path):
    path = tmp_path / 'mirror.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(path)], check=True)

    def git(*args, input=None):
        return subprocess.check_output(['git', '--git-dir', str(path), *args], input=input, text=True).strip()

    for key, value in {'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
                       'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}.items():
        monkeypatch.setenv(key, value)
    tree = git('mktree', input='')
    candidate = git('commit-tree', tree, input='original\n')
    head = git('commit-tree', tree, '-p', candidate, input='public main\n')
    foreign = git('commit-tree', tree, input='unrelated\n')
    git('update-ref', 'refs/heads/main', head)
    monkeypatch.setattr(release, 'public_main', lambda: head)
    assert release.sync_public_main(path, candidate) == head
    with pytest.raises(subprocess.CalledProcessError):
        release.sync_public_main(path, foreign)
