# proxy-auto-config

Parse a PAC (Proxy Auto-Config) file and evaluate its `FindProxyForURL` logic in a minimal JavaScript interpreter to determine the proxy for a given URL.

## Usage

```python
from proxy_auto_config import PACEvaluator, evaluate_pac

script = '''
function FindProxyForURL(url, host) {
    if (isPlainHostName(host)) return "DIRECT";
    return "PROXY proxy.internal:3128";
}
'''

# One-shot
result = evaluate_pac(script, "http://intranet/")
assert result.value == "DIRECT"
assert result.direct is True

# Or reuse the parsed script
ev = PACEvaluator(script)
result = ev.find_proxy_for_url("http://example.org/path")
assert result.value == "PROXY proxy.internal:3128"
assert result.proxies == ["PROXY proxy.internal:3128"]

# Call any function defined in the script
flag = ev.call_function("FindProxyForURL", "http://intranet/", "intranet")
```

`evaluate_pac(script, url, dns_resolver=None)` returns a `FindProxyForURLResult` with `.value` (raw string), `.direct` (bool), and `.proxies` (list of tokens). `PACEvaluator` exposes `.find_proxy_for_url(url)` and `.call_function(name, *args)`.

## Why this exists

Corporate environments ship PAC files containing arbitrary JavaScript. Running them through a full JS engine (Node, V8) is heavy and not always available. This library implements a focused subset of JavaScript — the constructs real PAC files actually use: variables, functions, if/else, the ternary operator, string and array methods, and the standard PAC helper functions (`isPlainHostName`, `shExpMatch`, `dnsDomainIs`, `isInNet`, `dnsResolve`, `isResolvable`, `localHostOrDomainIs`, `dnsDomainLevels`, `myIpAddress`).

The trade-off: this is not a complete JavaScript engine. It does not support objects with dynamic properties, `for`/`while`/`do` loops, `try`/`catch`, closures over mutable variables in loops, or `new`. PAC files that rely on those constructs will raise `PacRuntimeError` or `PacSyntaxError`. If you need full coverage, use a real JS runtime.

## The edge you will hit

DNS resolution (`dnsResolve`, `isInNet`, `isResolvable`, `myIpAddress`) calls `socket.getaddrinfo` by default, which performs real network lookups. For deterministic tests, inject a fake resolver:

```python
def fake_dns(host):
    return {"internal.host": "10.0.0.5"}.get(host, "")

ev = PACEvaluator(script, dns_resolver=fake_dns)
```

The `dnsDomainIs` helper follows the PAC spec: a host matches a domain if it equals the domain or ends with it. The domain `".example.org"` matches both `www.example.org` and `example.org`; `"example.org"` (no leading dot) matches `example.org` and `www.example.org` but not `notexample.org`.


## Acknowledgements

This library is standalone and depends only on the Python standard library.
