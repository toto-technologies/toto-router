<script>
  // The console's own auth entry — shown by +layout.svelte whenever /v1/auth/me is unauthed,
  // so a fresh visitor to /console can register + sign in right here (no /svelte detour).
  // Same-origin, cookie-session: login/register set the httpOnly toto_session cookie server-side;
  // on success we hard-navigate to the Overview so the shell re-runs its identity/org/usage loads.
  import { base } from '$app/paths';
  import { login, register, getMe } from '$lib/api/admin.js';
  import { ApiError } from '$lib/api/client.js';
  import { setOperatorToken, clearOperatorToken } from '$lib/oss-auth.js';

  // OSS single-tenant: no accounts — one operator token gates the console (Grafana/Jupyter pattern).
  // Statically foldable so the account/SSO form is dead-code-eliminated from the OSS bundle.
  const OSS = typeof __EDITION__ !== 'undefined' && __EDITION__ === 'oss';

  const MIN_PASSWORD_LEN = 8; // mirrors MIN_PASSWORD_LEN in routes/auth.py

  // --- OSS token gate ---
  let token = $state('');
  let tokenBusy = $state(false);
  let tokenError = $state('');

  async function submitToken(e) {
    e.preventDefault();
    tokenError = '';
    if (!token.trim()) {
      tokenError = 'Paste your gateway token to continue.';
      return;
    }
    tokenBusy = true;
    setOperatorToken(token.trim()); // the cookie the gateway reads; validate it with a probe
    try {
      const meResp = await getMe();
      if (!meResp?.is_operator) throw new ApiError(401, 'invalid_token', 'not the operator token');
      done();
    } catch {
      clearOperatorToken();
      tokenError = "That token wasn't accepted. Check TOTO_GW_AUTH_TOKEN and try again.";
    } finally {
      tokenBusy = false;
    }
  }

  let mode = $state('login'); // 'login' | 'register'
  let email = $state('');
  let password = $state('');
  let busy = $state(false);
  let error = $state('');
  let notice = $state('');

  const isRegister = $derived(mode === 'register');

  // SSO bounce-back: /v1/auth/sso/{start,callback} redirect here with ?sso_error=<code> on failure.
  let ssoNotice = $state('');
  if (typeof location !== 'undefined') {
    const code = new URLSearchParams(location.search).get('sso_error');
    if (code) ssoNotice = ssoMessage(code);
  }

  function ssoMessage(code) {
    switch (code) {
      case 'no_sso':
        return "That email domain doesn't have single sign-on set up — sign in with your password, or ask your admin.";
      case 'email':
        return 'Your identity provider did not return a verified email address.';
      case 'domain':
        return "Your identity-provider account isn't in this organization's allowed email domains.";
      case 'state':
        return 'Your sign-in link expired — please try again.';
      default:
        return 'Single sign-on could not be completed — please try again.';
    }
  }

  // Email-first SSO: resolve the org by the entered email's domain, then hand off to the IdP.
  // A full-page navigation is required — OAuth redirects must happen at the top level.
  function continueWithSSO() {
    error = '';
    if (!email.includes('@')) {
      error = 'Enter your work email to continue with SSO.';
      return;
    }
    const next = encodeURIComponent(base + '/');
    window.location.assign(`/v1/auth/sso/start?email=${encodeURIComponent(email)}&next=${next}`);
  }

  function setMode(m) {
    mode = m;
    error = '';
    notice = '';
  }

  async function submit(e) {
    e.preventDefault();
    error = '';
    notice = '';
    if (!email.includes('@')) {
      error = 'Enter a valid email address.';
      return;
    }
    if (isRegister && password.length < MIN_PASSWORD_LEN) {
      error = `Password must be at least ${MIN_PASSWORD_LEN} characters.`;
      return;
    }
    busy = true;
    try {
      if (isRegister) {
        await register(email, password);
        // Register is enumeration-safe (generic 200). Email-verify is OFF on this deploy, so a
        // fresh account can sign in immediately — auto-login. If a deploy turns verify ON, login
        // 403s with email_unverified and we prompt to check the inbox instead.
        try {
          await login(email, password);
          done();
        } catch (err) {
          if (err instanceof ApiError && err.code === 'email_unverified') {
            notice = 'Account created. Check your email to verify, then sign in.';
            setMode('login');
          } else {
            throw err;
          }
        }
      } else {
        await login(email, password);
        done();
      }
    } catch (err) {
      error = humanize(err);
    } finally {
      busy = false;
    }
  }

  function done() {
    // Cookie is set same-origin; a full navigation to the base index re-runs the shell's loads
    // and lands the user on Overview. Respects CONSOLE_BASE (base = '/console' in prod).
    window.location.assign(base + '/');
  }

  /** Map the gateway's {error:{code,message}} onto a plain, non-leaky line. */
  function humanize(err) {
    if (!(err instanceof ApiError)) return 'Network error — please try again.';
    if (err.code === 'invalid_credentials') return 'Invalid email or password.';
    if (err.code === 'invite_required') return 'Registration requires an invite code.';
    if (err.code === 'email_unverified') return 'Please verify your email before signing in.';
    if (err.status === 429) return 'Too many attempts — try again in a minute.';
    if (err.status === 400) return err.message || 'Check your details and try again.';
    return err.message || 'Something went wrong — please try again.';
  }
</script>

<div class="authgate">
  {#if OSS}
    <form class="authcard card" onsubmit={submitToken}>
      <div class="authbrand">
        <div class="glyph"></div>
        <div class="name"><b>TOTO</b> <span>Control</span></div>
      </div>

      <h1>Connect to your gateway</h1>
      <p class="authsub">Paste the gateway token (<code>TOTO_GW_AUTH_TOKEN</code>) to open the console.</p>

      <div class="field">
        <label for="auth-token">Gateway token</label>
        <input
          id="auth-token"
          type="password"
          autocomplete="off"
          bind:value={token}
          placeholder="paste your token"
          required
        />
      </div>

      {#if tokenError}
        <div class="authmsg err" role="alert">{tokenError}</div>
      {/if}

      <button class="btn primary authsubmit" type="submit" disabled={tokenBusy}>
        {tokenBusy ? 'Connecting…' : 'Connect'}
      </button>
    </form>
  {:else}
  <form class="authcard card" onsubmit={submit}>
    <div class="authbrand">
      <div class="glyph"></div>
      <div class="name"><b>TOTO</b> <span>Control</span></div>
    </div>

    <h1>{isRegister ? 'Create your account' : 'Sign in'}</h1>
    <p class="authsub">
      {isRegister
        ? 'Register to open the gateway control surface.'
        : 'Access the gateway control surface.'}
    </p>

    <div class="seg authseg" role="tablist" aria-label="Sign in or register">
      <button type="button" class:on={!isRegister} onclick={() => setMode('login')}>Sign in</button>
      <button type="button" class:on={isRegister} onclick={() => setMode('register')}>Register</button>
    </div>

    <div class="field">
      <label for="auth-email">Email</label>
      <input
        id="auth-email"
        type="email"
        autocomplete="email"
        bind:value={email}
        placeholder="you@company.com"
        required
      />
    </div>

    <div class="field">
      <label for="auth-password">Password</label>
      <input
        id="auth-password"
        type="password"
        autocomplete={isRegister ? 'new-password' : 'current-password'}
        bind:value={password}
        placeholder={isRegister ? `At least ${MIN_PASSWORD_LEN} characters` : '••••••••'}
        required
      />
    </div>

    {#if error}
      <div class="authmsg err" role="alert">{error}</div>
    {/if}
    {#if notice}
      <div class="authmsg ok" role="status">{notice}</div>
    {/if}
    {#if ssoNotice}
      <div class="authmsg err" role="alert">{ssoNotice}</div>
    {/if}

    <button class="btn primary authsubmit" type="submit" disabled={busy}>
      {busy ? 'Working…' : isRegister ? 'Create account' : 'Sign in'}
    </button>

    <div class="ssodiv"><span>or</span></div>
    <button type="button" class="btn ghost ssobtn" onclick={continueWithSSO} disabled={busy}>
      Continue with SSO
    </button>
  </form>
  {/if}
</div>

<style>
  .authgate {
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: var(--bg);
    background-image: radial-gradient(var(--grid) 1.2px, transparent 1.4px);
    background-size: 24px 24px;
    background-position: -1px -1px;
  }
  .authcard {
    width: 100%;
    max-width: 380px;
    padding: 26px 26px 24px;
  }
  .authbrand {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-bottom: 18px;
  }
  .authbrand .glyph {
    width: 22px;
    height: 22px;
    border-radius: 5px;
    flex: 0 0 auto;
    position: relative;
    background: linear-gradient(150deg, var(--accent), #567a37);
    box-shadow: 0 0 0 1px var(--accent-line), 0 0 14px -2px var(--accent-soft);
  }
  .authbrand .glyph::after {
    content: '';
    position: absolute;
    inset: 5px;
    border-radius: 2px;
    background: repeating-linear-gradient(0deg, rgba(0, 0, 0, 0.35) 0 1px, transparent 1px 3px);
  }
  .authbrand .name {
    font-weight: calc(650 + (var(--ui-weight) - 400));
    letter-spacing: 0.02em;
    font-size: 0.8125rem;
  }
  .authbrand .name b {
    color: var(--text);
  }
  .authbrand .name span {
    color: var(--text-3);
    font-weight: calc(600 + (var(--ui-weight) - 400));
  }
  h1 {
    margin: 0;
    font-size: 1.1875rem;
    font-weight: calc(650 + (var(--ui-weight) - 400));
    letter-spacing: -0.01em;
  }
  .authsub {
    margin: 4px 0 16px;
    color: var(--text-3);
    font-size: 0.75rem;
  }
  .authseg {
    display: flex;
    width: 100%;
    margin-bottom: 16px;
  }
  .authseg button {
    flex: 1;
  }
  .authsubmit {
    width: 100%;
    height: 38px;
    margin-top: 4px;
  }
  .authsubmit:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .authmsg {
    font-size: 0.75rem;
    padding: 9px 11px;
    border-radius: 8px;
    margin-bottom: 12px;
  }
  .authmsg.err {
    color: var(--crit);
    background: var(--crit-soft);
    border: 1px solid rgba(213, 55, 66, 0.28);
  }
  .authmsg.ok {
    color: var(--good);
    background: var(--good-soft);
    border: 1px solid rgba(30, 158, 74, 0.28);
  }
  .ssodiv {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 14px 0 12px;
    color: var(--text-3);
    font-size: 0.6875rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .ssodiv::before,
  .ssodiv::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--line);
  }
  .ssobtn {
    width: 100%;
    height: 38px;
  }
</style>
