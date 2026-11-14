// Sign-in page: a passkey button when any are registered, and the recovery
// password form only when ADMIN_PASSWORD_LOGIN is switched on.

import { supported as passkeysSupported, signInWithPasskey } from './webauthn.js';

const $ = (s) => document.querySelector(s);

function showError(msg) {
  const el = $('[data-error]');
  el.textContent = msg;
  el.hidden = !msg;
}

function done() {
  location.replace('/');
}

async function init() {
  let status;
  try {
    const res = await fetch('/api/auth/status');
    status = await res.json();
  } catch {
    $('[data-intro]').textContent = 'The admin console is not responding. Try again shortly.';
    return;
  }
  if (status.signedIn) { done(); return; }

  const canPasskey = status.passkeys > 0 && passkeysSupported();
  $('[data-passkey-area]').hidden = !canPasskey;
  $('[data-password-form]').hidden = !status.passwordLogin;
  $('[data-or]').hidden = !(canPasskey && status.passwordLogin);
  $('[data-locked]').hidden = status.passkeys > 0 || status.passwordLogin;

  if (canPasskey) {
    $('[data-intro]').textContent = 'Use the passkey on this device, your phone, or a security key.';
  } else if (status.passwordLogin) {
    $('[data-intro]').textContent = status.passkeys > 0
      ? 'This browser cannot use passkeys. Sign in with the recovery password.'
      : 'No passkeys yet. Sign in with the recovery password, then register one.';
  } else if (status.passkeys > 0) {
    $('[data-intro]').textContent = 'This browser does not support passkeys. Try a current version of Chrome, Edge, Safari or Firefox.';
  } else {
    $('[data-intro]').textContent = '';
  }

  $('[data-passkey-btn]').addEventListener('click', async (e) => {
    showError('');
    e.currentTarget.disabled = true;
    try {
      await signInWithPasskey();
      done();
    } catch (err) {
      showError(err.message);
    } finally {
      e.currentTarget.disabled = false;
    }
  });

  $('[data-password-form]').addEventListener('submit', async (e) => {
    e.preventDefault();
    showError('');
    const form = e.currentTarget;
    const btn = form.querySelector('button');
    btn.disabled = true;
    try {
      const res = await fetch('/api/session/password', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: form.password.value }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `Sign-in failed (HTTP ${res.status}).`);
      }
      done();
    } catch (err) {
      showError(err.message);
      form.password.select();
    } finally {
      btn.disabled = false;
    }
  });
}

init();
