"""借用浏览器的薄会话合同；页面知识只由打包的站点适配器提供。"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
import uuid

from runtime.browser_link import BrowserLink, _read_state
from runtime.file_write_lock import file_write_lock
from runtime.paths import runtime_state_dir
from runtime.state_file import write_state


@dataclass
class NativeSite:
    platform: str
    display: str
    home: str
    login_url: str
    authenticated: object

    def bridge(self):
        return BrowserLink()

    def transport_name(self):
        return 'content_script_native_messaging'

    def resource_name(self):
        return 'browser_link:' + self.platform

    def state_path(self):
        return runtime_state_dir() / 'browser_link' / self.platform / 'interaction.json'

    def state(self):
        try:
            value = json.loads(self.state_path().read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {'risk': True, 'reason': 'interaction_state_invalid'}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {'risk': True, 'reason': 'interaction_state_invalid'}

    def failure(self, code, **extra):
        kind = {'SECURITY_BLOCK': 'anti_bot_verification', 'AUTH_REQUIRED': 'login_required',
                'risk_latched': 'anti_bot_verification', 'outcome_latched': 'request_outcome_unknown'}.get(code, code)
        manual = kind in {'anti_bot_verification', 'login_required', 'request_outcome_unknown', 'pending_manual_interaction'}
        return {'ok': False, 'platform': self.display, 'status': 'needs_interaction' if manual else 'failed',
                'failure_type': kind, 'error': {'type': kind, 'failure_type': kind, 'manual_action_required': manual},
                'manual_action_required': manual, 'items': [], 'retryable': False,
                'resume_policy': 'stop_no_auto_resume' if kind in {'anti_bot_verification', 'request_outcome_unknown'} else 'explicit_retry',
                'metadata': {'transport': self.transport_name(), 'ownership': 'borrowed'}, **extra}

    def admission(self):
        state = self.state()
        reason = ('SECURITY_BLOCK' if state.get('risk') else 'outcome_latched' if state.get('unknown_outcome')
                  else 'pending_manual_interaction' if state.get('expires_at') and not state.get('cancelled') else 'ok')
        # Transport availability is not account authentication or remote rate state.
        return {'allowed': reason == 'ok', 'reason': reason, 'manual_action_required': reason != 'ok'}

    def invoke(self, operation, *, url='', **payload):
        if operation not in {'login', 'attention', 'probe', 'release_login', 'release', 'auth_verify'}:
            gate = self.admission()
            if not gate['allowed']:
                return self.failure(gate['reason'], request_executed=False)
        response = self.bridge().request(operation, {'url': url, **payload}, platform=self.platform)
        data = response.get('data') or {}
        code = response.get('code') or response.get('failure_type') or ''
        if operation in {'search', 'detail', 'comments'}:
            from platform_adapters.common import defer_browser_busy
            defer_browser_busy(self.platform, code, request_executed=response.get('request_executed'), resource=self.resource_name())
        if data.get('risk') or code == 'SECURITY_BLOCK':
            code = 'SECURITY_BLOCK'
        elif data.get('login') or data.get('detail_login_required'):
            code = 'AUTH_REQUIRED'
        if code and (not response.get('ok') or code in {'SECURITY_BLOCK', 'AUTH_REQUIRED'}):
            uncertain = response.get('request_executed') is not False and code in {
                'native_command_timeout', 'native_host_unreachable', 'browser_disconnected', 'native_host_disconnected',
                'opencli_caller_timeout', 'COMMAND_RESULT_UNKNOWN'}
            if code == 'SECURITY_BLOCK' or uncertain:
                with file_write_lock(self.state_path()):
                    current = self.state()
                    # Late results must not clear a newer cancellation or risk observation.
                    write_state(self.state_path(), {**current, 'risk': bool(current.get('risk') or code == 'SECURITY_BLOCK'),
                        'unknown_outcome': bool(current.get('unknown_outcome') or uncertain), 'reason': code, 'at': time.time()})
            result = self.failure('outcome_latched' if uncertain else code,
                request_executed=response.get('request_executed', False))
            result['metadata']['transport_failure'] = code
            return result
        response.setdefault('metadata', {}).update(transport=self.transport_name(), ownership='borrowed',
            browser_process_owned=False, platform=self.platform)
        response['platform'] = self.display
        return response

    def request_login(self, *, activate=False):
        with file_write_lock(self.state_path()):
            current = self.state()
            if current.get('expires_at') and not current.get('cancelled') and not activate:
                return {'status': 'waiting_for_user', 'platform': self.platform, 'expires_at': current['expires_at']}
            interaction = uuid.uuid4().hex
            write_state(self.state_path(), {**current, 'interaction_id': interaction, 'cancelled': False,
                'expires_at': time.time() + 300})
        attention = bool(current.get('expires_at') and not current.get('cancelled'))
        response = self.invoke('attention' if attention else 'login', url='' if attention else self.login_url)
        if attention and response.get('failure_type') == 'work_tab_unavailable':
            response = self.invoke('login', url=self.login_url)
        if not response.get('ok') and response.get('failure_type') not in {'login_required', 'anti_bot_verification'}:
            with file_write_lock(self.state_path()):
                latest = self.state()
                if latest.get('interaction_id') == interaction:
                    write_state(self.state_path(), {**latest, 'cancelled': True, 'expires_at': None})
            return response
        return {'status': 'waiting_for_user', 'platform': self.platform, 'expires_at': self.state().get('expires_at'),
            'hint': f'在日常Chrome的{self.display}标签完成登录或验证后确认继续；等待不占执行槽，不重放原请求。'}

    def complete_login(self):
        observed = self.state()
        if observed.get('cancelled'):
            return {'status': 'not_verified', 'failure_type': 'manual_interaction_cancelled'}
        result = self.invoke('auth_verify', url=self.home)
        if not result.get('ok') or not self.authenticated(result.get('data', {})):
            return {'status': 'not_verified', 'platform': self.platform,
                'failure_type': result.get('failure_type') or 'auth_state_unconfirmed'}
        released = self.invoke('release_login')
        if not released.get('ok'):
            return {'status': 'not_verified', 'failure_type': released.get('failure_type') or 'manual_hold_release_failed'}
        with file_write_lock(self.state_path()):
            if self.state() != observed:
                return {'status': 'not_verified', 'failure_type': 'interaction_changed_during_probe'}
            write_state(self.state_path(), {'verified_at': time.time(), 'auth_generation': uuid.uuid4().hex})
        return {'status': 'ok', 'platform': self.platform, 'auth_state': 'authenticated'}

    def cancel_login(self):
        with file_write_lock(self.state_path()):
            write_state(self.state_path(), {**self.state(), 'cancelled': True})
        self.invoke('release_login')
        return {'status': 'cancelled', 'platform': self.platform}

    def probe_auth(self):
        if self.state().get('expires_at') and not self.state().get('cancelled'):
            return {'status': 'waiting_for_user', 'auth_state': 'unknown', 'platform': self.platform}
        # Never navigate, refresh or open a login window during a background auth probe.
        result = self.invoke('probe')
        ready = result.get('ok') and self.authenticated(result.get('data', {}))
        return {'status': 'ok' if ready else 'unknown', 'auth_state': 'authenticated' if ready else 'unknown',
                'platform': self.platform, 'failure_type': result.get('failure_type'), 'manual_action_required': False}

    def interaction_status(self):
        current = self.state()
        if current.get('risk') or current.get('unknown_outcome') or current.get('cancelled'):
            return 'blocked'
        return 'waiting' if current.get('expires_at') else 'ready' if current.get('verified_at') else 'blocked'

    def runtime_identity(self):
        from runtime.browser_channel import browser_resource_contract
        state = _read_state()
        contract = browser_resource_contract(ownership='borrowed', resource_id='browser_link:' + self.platform,
            generation=str(state.get('generation') or ''), driver='content_script_native_messaging')
        return {**contract, 'auth_generation': self.state().get('auth_generation', ''),
            'profile': hashlib.sha256(str(state.get('profile_id', '')).encode()).hexdigest(), 'protocol': 1}

    def cancel_task(self, task_id):
        return self.bridge().cancel_task(task_id)
