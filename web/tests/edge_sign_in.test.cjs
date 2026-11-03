"use strict";

const assert = require("node:assert/strict");

const {set_global, zrequire} = require("./lib/namespace.cjs");
const {run_test} = require("./lib/test.cjs");

const edge_sign_in = zrequire("edge_sign_in");

function fake_session_storage() {
    const items = new Map();
    return {
        getItem: (key) => items.get(key) ?? null,
        setItem(key, value) {
            items.set(key, value);
        },
    };
}

function respond_with(type) {
    const requests = [];
    set_global("fetch", async (url, options) => {
        requests.push({url, options});
        return {type};
    });
    return requests;
}

run_test("edge redirect reloads once, then blocks within the guard window", async () => {
    set_global("sessionStorage", fake_session_storage());
    const requests = respond_with("opaqueredirect");

    assert.equal(await edge_sign_in.verdict_after_network_error(1000), "reload");
    assert.equal(requests.length, 1);
    const [{url, options}] = requests;
    assert.equal(url, "/api/v1/server_settings");
    assert.equal(options.redirect, "manual");
    assert.equal(options.credentials, "same-origin");
    assert.ok(options.signal instanceof AbortSignal);

    const still_inside_window = 1000 + edge_sign_in.RELOAD_GUARD_MS - 1;
    assert.equal(await edge_sign_in.verdict_after_network_error(still_inside_window), "blocked");

    const after_window = 1000 + edge_sign_in.RELOAD_GUARD_MS;
    assert.equal(await edge_sign_in.verdict_after_network_error(after_window), "reload");
});

run_test("a reachable server means a real network blip", async () => {
    set_global("sessionStorage", fake_session_storage());
    respond_with("basic");
    assert.equal(await edge_sign_in.verdict_after_network_error(1000), "retry");
});

run_test("a failing probe means the connection is really down", async () => {
    set_global("sessionStorage", fake_session_storage());
    set_global("fetch", async () => {
        throw new TypeError("Failed to fetch");
    });
    assert.equal(await edge_sign_in.verdict_after_network_error(1000), "retry");
});

run_test("no usable sessionStorage never reloads", async () => {
    set_global("sessionStorage", {
        getItem() {
            throw new Error("SecurityError");
        },
        setItem() {
            throw new Error("SecurityError");
        },
    });
    respond_with("opaqueredirect");
    assert.equal(await edge_sign_in.verdict_after_network_error(1000), "blocked");
});
