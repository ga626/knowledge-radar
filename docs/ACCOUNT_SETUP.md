# Account And Browser Profile Setup

Some platforms require interactive login and persistent browser profiles. KnowledgeRadar cannot and should not bypass QR-code login, CAPTCHA, device verification, or platform risk controls.

Run:

```bat
scripts\setup_accounts.bat
```

The helper opens the selected platform login page and waits for you to finish. Before login/profile setup, platform checks may be reported as needing interaction or configuration. After setup, rerun verification; configured platforms should pass, and any remaining degradation should be investigated.

## Platforms

| Platform | Setup expectation | First-run verification |
| --- | --- | --- |
| Xiaohongshu | Interactive browser login/profile; optional probe after setup | Needs interaction before login; should pass after configured |
| Zhihu | Interactive browser login/profile | Needs interaction before login; should pass after configured |
| BOSS | Interactive browser login and possible security verification | Needs interaction/security handling before login; should pass after configured |
| Liepin | Interactive browser login when required | Needs interaction before login when the site asks; should pass after configured |
| Maimai | Interactive browser login when required | Uses web fallback when browser search is unavailable; remaining failures should be investigated |

Profile examples live in `config\profile_registry.example.json`. Real profile state belongs in `config\profile_registry.json` or `local\profiles\`, both local-only.
