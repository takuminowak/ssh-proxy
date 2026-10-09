import socket
import unittest
import urllib.parse

from proxy_auto_config import (
    PACEvaluator,
    PacRuntimeError,
    PacSyntaxError,
    Proxies,
    evaluate_pac,
)


class TestBasic(unittest.TestCase):
    def test_direct_only(self):
        script = """
        function FindProxyForURL(url, host) {
            return "DIRECT";
        }
        """
        r = evaluate_pac(script, "http://example.org/path")
        self.assertEqual(r.value, "DIRECT")
        self.assertTrue(r.direct)
        self.assertEqual(list(r.proxies), ["DIRECT"])

    def test_proxy_host_port(self):
        script = """
        function FindProxyForURL(url, host) {
            return "PROXY proxy.internal:8080";
        }
        """
        r = evaluate_pac(script, "http://example.org/")
        self.assertFalse(r.direct)
        self.assertEqual(r.proxies[0], "PROXY proxy.internal:8080")

    def test_multiple_proxies_semicolon(self):
        script = """
        function FindProxyForURL(url, host) {
            return "PROXY a:80; PROXY b:8080; DIRECT";
        }
        """
        r = evaluate_pac(script, "http://example.org/")
        self.assertEqual(len(r.proxies), 3)
        self.assertTrue(r.direct)


class TestConditions(unittest.TestCase):
    def test_if_plain_hostname(self):
        script = """
        function FindProxyForURL(url, host) {
            if (isPlainHostName(host))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://intranet/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")

    def test_shExpMatch(self):
        script = """
        function FindProxyForURL(url, host) {
            if (shExpMatch(url, "*://*.local/*"))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://site.local/path").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/path").value, "PROXY proxy.internal:3128")

    def test_conditional_expression(self):
        script = """
        function FindProxyForURL(url, host) {
            return isPlainHostName(host) ? "DIRECT" : "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://intranet/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")

    def test_logical_or_and(self):
        script = """
        function FindProxyForURL(url, host) {
            if (isPlainHostName(host) || shExpMatch(host, "*.local"))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://intranet/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://site.local/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")


class TestHelpers(unittest.TestCase):
    def test_dnsDomainIs(self):
        script = """
        function FindProxyForURL(url, host) {
            if (dnsDomainIs(host, ".example.org"))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://www.example.org/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://other.org/").value, "PROXY proxy.internal:3128")

    def test_localHostOrDomainIs(self):
        script = """
        function FindProxyForURL(url, host) {
            if (localHostOrDomainIs(host, "www.example.org"))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://www.example.org/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://www/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://other.org/").value, "PROXY proxy.internal:3128")

    def test_isInNet(self):
        def fake_dns(host):
            mapping = {
                "internal.host": "10.0.0.5",
                "external.host": "8.8.8.8",
            }
            return mapping.get(host, "")
        script = """
        function FindProxyForURL(url, host) {
            if (isInNet(host, "10.0.0.0", "255.0.0.0"))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script, dns_resolver=fake_dns)
        self.assertEqual(ev.find_proxy_for_url("http://internal.host/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://external.host/").value, "PROXY proxy.internal:3128")

    def test_isResolvable(self):
        def fake_dns(host):
            return "1.2.3.4" if host == "resolvable.host" else ""
        script = """
        function FindProxyForURL(url, host) {
            if (isResolvable(host))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script, dns_resolver=fake_dns)
        self.assertEqual(ev.find_proxy_for_url("http://resolvable.host/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://unresolvable.host/").value, "PROXY proxy.internal:3128")

    def test_dnsResolve(self):
        def fake_dns(host):
            return "192.168.1.1" if host == "target.host" else ""
        script = """
        function FindProxyForURL(url, host) {
            var ip = dnsResolve(host);
            if (ip == "192.168.1.1")
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script, dns_resolver=fake_dns)
        self.assertEqual(ev.find_proxy_for_url("http://target.host/").value, "DIRECT")


class TestStrings(unittest.TestCase):
    def test_string_methods(self):
        script = """
        function FindProxyForURL(url, host) {
            var idx = url.indexOf("://");
            var scheme = url.substring(0, idx);
            if (scheme == "https")
                return "PROXY secure.proxy:8443";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("https://example.org/").value, "PROXY secure.proxy:8443")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")

    def test_string_concat_plus(self):
        script = """
        function FindProxyForURL(url, host) {
            return "PROXY " + host + ":8080";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY example.org:8080")

    def test_toLowerCase(self):
        script = """
        function FindProxyForURL(url, host) {
            if (host.toLowerCase() == "example.org")
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://EXAMPLE.ORG/").value, "DIRECT")


class TestArithmetic(unittest.TestCase):
    def test_dnr_domain_levels(self):
        script = """
        function FindProxyForURL(url, host) {
            var levels = dnsDomainLevels(host);
            if (levels > 1)
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://a.b.example.org/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example/").value, "PROXY proxy.internal:3128")

    def test_modulo(self):
        script = """
        function FindProxyForURL(url, host) {
            var h = host.length % 2;
            if (h == 0)
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://ab/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://abc/").value, "PROXY proxy.internal:3128")


class TestArrays(unittest.TestCase):
    def test_array_literal_and_join(self):
        script = """
        function FindProxyForURL(url, host) {
            var arr = ["PROXY", "proxy.internal:3128"];
            return arr.join(" ");
        }
        """
        ev = PACEvaluator(script)
        r = ev.find_proxy_for_url("http://example.org/")
        self.assertEqual(r.value, "PROXY proxy.internal:3128")

    def test_array_indexing(self):
        script = """
        function FindProxyForURL(url, host) {
            var arr = ["a", "b", "c"];
            return arr[1] == "b" ? "DIRECT" : "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")


class TestUserFunctions(unittest.TestCase):
    def test_call_user_function_directly(self):
        script = """
        function isPrivate(host) {
            return shExpMatch(host, "10.*");
        }
        function FindProxyForURL(url, host) {
            if (isPrivate(host))
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        result = ev.call_function("isPrivate", "10.0.0.1")
        self.assertTrue(result)
        result2 = ev.call_function("isPrivate", "8.8.8.8")
        self.assertFalse(result2)

    def test_recursive_factorial(self):
        script = """
        function fact(n) {
            if (n <= 1)
                return 1;
            return n * fact(n - 1);
        }
        function FindProxyForURL(url, host) {
            if (fact(5) == 120)
                return "DIRECT";
            return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.call_function("fact", 5), 120)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")


class TestEdgeCases(unittest.TestCase):
    def test_empty_result(self):
        script = """
        function FindProxyForURL(url, host) {
            return "";
        }
        """
        r = evaluate_pac(script, "http://example.org/")
        self.assertEqual(r.value, "")
        self.assertFalse(r.direct)
        self.assertEqual(list(r.proxies), [])

    def test_no_findproxy_function_raises(self):
        script = "var x = 1;"
        with self.assertRaises(PacSyntaxError):
            PACEvaluator(script)

    def test_syntax_error_bad_token(self):
        script = "function FindProxyForURL(url, host) { return # }"
        with self.assertRaises(PacSyntaxError):
            PACEvaluator(script)

    def test_unterminated_block_comment(self):
        script = "/* unterminated function FindProxyForURL(url, host) { return \"DIRECT\"; }"
        # A block comment without closing */ is consumed by the regex to EOF —
        # so FindProxyForURL never gets defined and we get a syntax error
        # about the missing function.
        with self.assertRaises(PacSyntaxError):
            PACEvaluator(script)

    def test_unsupported_feature_raises(self):
        script = """
        function FindProxyForURL(url, host) {
            return new Object();
        }
        """
        with self.assertRaises(PacSyntaxError):
            PACEvaluator(script)

    def test_undefined_function_in_script(self):
        script = """
        function FindProxyForURL(url, host) {
            return nonexistentHelper(host);
        }
        """
        ev = PACEvaluator(script)
        with self.assertRaises(PacRuntimeError):
            ev.find_proxy_for_url("http://example.org/")

    def test_var_assignment_mutation(self):
        script = """
        function FindProxyForURL(url, host) {
            var result = "DIRECT";
            if (!isPlainHostName(host)) {
                result = "PROXY proxy.internal:3128";
            }
            return result;
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://intranet/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")

    def test_else_if_chain(self):
        script = """
        function FindProxyForURL(url, host) {
            if (isPlainHostName(host))
                return "DIRECT";
            else if (shExpMatch(host, "*.local"))
                return "DIRECT";
            else
                return "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://intranet/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://site.local/").value, "DIRECT")
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY proxy.internal:3128")

    def test_url_without_host(self):
        script = """
        function FindProxyForURL(url, host) {
            return host == "" ? "DIRECT" : "PROXY proxy.internal:3128";
        }
        """
        ev = PACEvaluator(script)
        # A URL like "file:///path" has no hostname.
        self.assertEqual(ev.find_proxy_for_url("file:///path/to/thing").value, "DIRECT")

    def test_alert_does_not_crash(self):
        script = """
        function FindProxyForURL(url, host) {
            alert("debug message");
            return "DIRECT";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")

    def test_console_log_does_not_crash(self):
        script = """
        function FindProxyForURL(url, host) {
            console.log("debug");
            return "DIRECT";
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")


class TestParsing(unittest.TestCase):
    def test_block_comments(self):
        script = """
        /* This is a comment */
        function FindProxyForURL(url, host) {
            // line comment
            return "DIRECT"; /* trailing */
        }
        """
        r = evaluate_pac(script, "http://example.org/")
        self.assertEqual(r.value, "DIRECT")

    def test_nested_function_calls(self):
        script = """
        function getProxy(host) {
            return "PROXY " + host + ":80";
        }
        function FindProxyForURL(url, host) {
            return getProxy(host);
        }
        """
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "PROXY example.org:80")

    def test_string_escape_sequences(self):
        script = r'''
        function FindProxyForURL(url, host) {
            var s = "a\tb";
            return s == "a\tb" ? "DIRECT" : "PROXY proxy.internal:3128";
        }
        '''
        ev = PACEvaluator(script)
        self.assertEqual(ev.find_proxy_for_url("http://example.org/").value, "DIRECT")


if __name__ == "__main__":
    unittest.main()
