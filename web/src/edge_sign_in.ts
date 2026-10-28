// When Zulip runs behind a single sign-on edge (the Moneta FOSS bundle's
// mPass ForwardAuth), an expired edge session turns every request into a
// cross-origin redirect to the sign-in host.  The browser refuses to
// follow it for XHR and reports status 0, which looks exactly like a
// network error, so the tab would retry forever.  A same-origin probe
// with redirect: "manual" tells the two apart.

const PROBE_URL = "/api/v1/server_settings";
const RELOAD_GUARD_KEY = "zulip_edge_sign_in_reload_at";
export const RELOAD_GUARD_MS = 5 * 60 * 1000;

export type NetworkErrorVerdict = "reload" | "blocked" | "retry";

export async function edge_redirects_to_sign_in(): Promise<boolean> {
    try {
        const response = await fetch(PROBE_URL, {redirect: "manual", credentials: "same-origin"});
        return response.type === "opaqueredirect";
    } catch {
        return false;
    }
}

function last_reload_ms(): number | undefined {
    try {
        const value = window.sessionStorage.getItem(RELOAD_GUARD_KEY);
        return value === null ? undefined : Number(value);
    } catch {
        return undefined;
    }
}

function record_reload(now_ms: number): boolean {
    try {
        window.sessionStorage.setItem(RELOAD_GUARD_KEY, String(now_ms));
        return true;
    } catch {
        return false;
    }
}

function reloaded_recently(now_ms: number): boolean {
    const last_ms = last_reload_ms();
    return last_ms !== undefined && now_ms - last_ms < RELOAD_GUARD_MS;
}

// Without a usable sessionStorage the loop guard cannot hold, so the
// tab stays put rather than risk a reload loop.
export async function verdict_after_network_error(
    now_ms: number = Date.now(),
): Promise<NetworkErrorVerdict> {
    if (!(await edge_redirects_to_sign_in())) {
        return "retry";
    }
    if (reloaded_recently(now_ms) || !record_reload(now_ms)) {
        return "blocked";
    }
    return "reload";
}
