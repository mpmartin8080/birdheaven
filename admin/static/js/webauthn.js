// Passkey encode/decode glue between the browser WebAuthn API (ArrayBuffers)
// and the JSON the admin service exchanges (base64url strings, as
// python-webauthn uses). Adapted from financesite/webroot/js/webauthn.js;
// used by login.js (signing in) and admin.js (registering a passkey).

function b64urlToBuf(s) {
    const pad = s.length % 4 === 0 ? '' : '='.repeat(4 - (s.length % 4));
    const base64 = (s + pad).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const buf = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) buf[i] = raw.charCodeAt(i);
    return buf.buffer;
}

function bufToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let str = '';
    for (const b of bytes) str += String.fromCharCode(b);
    return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/** True when this browser can attempt a passkey ceremony at all. */
export function supported() {
    return typeof window !== 'undefined'
        && typeof window.PublicKeyCredential === 'function'
        && typeof navigator !== 'undefined'
        && typeof navigator.credentials === 'object'
        && navigator.credentials !== null;
}

/** The service's creation-options JSON -> what navigator.credentials.create() needs. */
function decodeCreationOptions(json) {
    return {
        ...json,
        challenge: b64urlToBuf(json.challenge),
        user: { ...json.user, id: b64urlToBuf(json.user.id) },
        excludeCredentials: (json.excludeCredentials || []).map((c) => ({
            ...c, id: b64urlToBuf(c.id),
        })),
    };
}

/** The service's request-options JSON -> what navigator.credentials.get() needs. */
function decodeRequestOptions(json) {
    return {
        ...json,
        challenge: b64urlToBuf(json.challenge),
        allowCredentials: (json.allowCredentials || []).map((c) => ({
            ...c, id: b64urlToBuf(c.id),
        })),
    };
}

/** A newly created credential, in the shape the service's verify endpoint expects. */
function encodeAttestation(cred) {
    return {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        response: {
            clientDataJSON: bufToB64url(cred.response.clientDataJSON),
            attestationObject: bufToB64url(cred.response.attestationObject),
            transports: cred.response.getTransports ? cred.response.getTransports() : [],
        },
        clientExtensionResults: cred.getClientExtensionResults
            ? cred.getClientExtensionResults() : {},
    };
}

/** A signed assertion, in the shape the service's verify endpoint expects. */
function encodeAssertion(cred) {
    return {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        response: {
            clientDataJSON: bufToB64url(cred.response.clientDataJSON),
            authenticatorData: bufToB64url(cred.response.authenticatorData),
            signature: bufToB64url(cred.response.signature),
            userHandle: cred.response.userHandle ? bufToB64url(cred.response.userHandle) : null,
        },
        clientExtensionResults: cred.getClientExtensionResults
            ? cred.getClientExtensionResults() : {},
    };
}

function describeCeremonyError(err, phase) {
    if (err.name === 'NotAllowedError') {
        return phase === 'register'
            ? 'Cancelled, or the device declined.'
            : 'Cancelled, or no matching passkey was found on this device.';
    }
    if (err.name === 'InvalidStateError') {
        return 'That device already has a passkey registered here.';
    }
    return err.message || String(err);
}

/**
 * Register a new passkey for the signed-in session.
 *
 * `fetchOptions` and `verify` are the two API calls this needs, passed in rather
 * than imported so this module does not have to know whether the caller uses
 * api.js or a raw fetch. `fetchOptions()` resolves to the parsed JSON body of
 * `POST /api/webauthn/register/options`; `verify(credential)` should POST it to
 * `/api/webauthn/register/verify` and resolve to whatever that returns.
 */
export async function registerPasskey({ fetchOptions, verify }) {
    const options = decodeCreationOptions(await fetchOptions());
    let credential;
    try {
        credential = await navigator.credentials.create({ publicKey: options });
    } catch (err) {
        throw new Error(describeCeremonyError(err, 'register'));
    }
    return verify(encodeAttestation(credential));
}

async function errorMessage(res) {
    try {
        const body = await res.json();
        if (body && body.error) return body.error;
    } catch { /* not JSON; the status line below will do */ }
    return `Passkey sign-in failed (HTTP ${res.status}).`;
}

/**
 * Sign in with a passkey. Raw fetch, deliberately not routed through api.js --
 * see login.js's own header comment for why signing in has to work standalone.
 * Resolves once the session cookie has been issued; throws a message meant to be
 * shown as-is otherwise.
 */
export async function signInWithPasskey() {
    const optRes = await fetch('/api/webauthn/login/options', {
        method: 'POST', headers: { accept: 'application/json' },
    });
    if (!optRes.ok) throw new Error(await errorMessage(optRes));
    const options = decodeRequestOptions(await optRes.json());

    let credential;
    try {
        credential = await navigator.credentials.get({ publicKey: options });
    } catch (err) {
        throw new Error(describeCeremonyError(err, 'login'));
    }

    const verifyRes = await fetch('/api/webauthn/login/verify', {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'application/json' },
        body: JSON.stringify({ credential: encodeAssertion(credential) }),
    });
    if (!verifyRes.ok) throw new Error(await errorMessage(verifyRes));
}
