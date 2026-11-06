using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Security.Principal;
using System.Text.RegularExpressions;

// Chrome owns this process. No service, debugger, account store or site parser.
internal static class BrowserLinkHost
{
    private const int Protocol = 1;
    private static readonly object OutputLock = new object(), StateLock = new object(), AdmissionLock = new object();
    private static readonly string Generation = Guid.NewGuid().ToString("N"), Token = Guid.NewGuid().ToString("N");
    private static readonly string StateFile = StateLocation();
    private static readonly ConcurrentDictionary<string, PendingRequest> Pending = new ConcurrentDictionary<string, PendingRequest>();
    private static readonly ConcurrentDictionary<string, long> CancelledTasks = new ConcurrentDictionary<string, long>();
    private static readonly ConcurrentDictionary<string, long> Seen = new ConcurrentDictionary<string, long>();
    private static readonly HashSet<string> Operations = new HashSet<string> { "search", "detail", "comments", "login", "attention", "probe", "auth_verify", "release", "release_login" };
    private static HttpListener Listener;
    private static int Port;
    private static volatile bool Connected, Draining;
    private static string ProfileId = "", ExtensionVersion = "";
    private static string StateLocation()
    {
        string explicitPath = Environment.GetEnvironmentVariable("KR_BROWSER_LINK_STATE_FILE");
        if (!String.IsNullOrEmpty(explicitPath)) return explicitPath;
        string settings = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "host-settings.json");
        if (File.Exists(settings)) {
            var value = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(settings)) as Dictionary<string, object>;
            if (value != null && value.ContainsKey("state_file")) return Convert.ToString(value["state_file"]);
        }
        return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "KnowledgeRadar", "data", "runtime", "browser_link", "host.json");
    }
    private sealed class PendingRequest
    {
        public readonly ManualResetEventSlim Done = new ManualResetEventSlim(false);
        public string TaskId, Platform;
        public Dictionary<string, object> Result;
        public bool Finish(Dictionary<string, object> result)
        { lock (this) { if (Result != null) return false; Result = result; Done.Set(); return true; } }
    }
    private static JavaScriptSerializer Serializer() { return new JavaScriptSerializer { MaxJsonLength = 1024 * 1024, RecursionLimit = 64 }; }
    private static string Text(Dictionary<string, object> value, string key)
    { object result; return value != null && value.TryGetValue(key, out result) ? Convert.ToString(result) : ""; }
    private static Dictionary<string, object> Failure(string code, string id, bool executed)
    { return new Dictionary<string, object> { { "ok", false }, { "code", code }, { "failure_type", code }, { "request_id", id }, { "request_executed", executed }, { "generation", Generation } }; }
    private static void Send(object value)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(Serializer().Serialize(value));
        if (bytes.Length > 1024 * 1024) throw new InvalidDataException("native_message_too_large");
        lock (OutputLock) { Stream output = Console.OpenStandardOutput(); output.Write(BitConverter.GetBytes(bytes.Length), 0, 4); output.Write(bytes, 0, bytes.Length); output.Flush(); }
    }
    private static byte[] ReadExact(Stream stream, int size)
    {
        byte[] bytes = new byte[size]; int offset = 0;
        while (offset < size) { int n = stream.Read(bytes, offset, size - offset); if (n <= 0) return null; offset += n; }
        return bytes;
    }
    private static Dictionary<string, object> ReadMessage()
    {
        Stream stream = Console.OpenStandardInput(); byte[] header = ReadExact(stream, 4);
        if (header == null) return null;
        int size = BitConverter.ToInt32(header, 0); if (size <= 0 || size > 1024 * 1024) throw new InvalidDataException("native_message_too_large");
        byte[] body = ReadExact(stream, size);
        return body == null ? null : Serializer().DeserializeObject(Encoding.UTF8.GetString(body)) as Dictionary<string, object>;
    }
    private static void AtomicJson(string path, object value)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        string temp = path + "." + Guid.NewGuid().ToString("N") + ".pending";
        File.WriteAllText(temp, Serializer().Serialize(value), new UTF8Encoding(false));
        if (File.Exists(path)) File.Replace(temp, path, null); else File.Move(temp, path);
    }
    private static void WriteState()
    {
        if (ProfileId == "") return;
        lock (StateLock) AtomicJson(StateFile, new Dictionary<string, object> {
            { "port", Port }, { "token", Token }, { "connected", Connected }, { "protocol", Protocol },
            { "generation", Generation }, { "profile_id", ProfileId }, { "extension_version", ExtensionVersion },
            { "pid", System.Diagnostics.Process.GetCurrentProcess().Id }, { "host_path", System.Reflection.Assembly.GetExecutingAssembly().Location },
            { "updated_at", DateTimeOffset.UtcNow.ToUnixTimeSeconds() } });
    }
    private static bool Hello(Dictionary<string, object> message)
    {
        string code = "", candidate = Text(message, "profile_id"); Guid profile;
        string pairFile = Path.Combine(Path.GetDirectoryName(StateFile), "paired-profile.json");
        if (Text(message, "protocol") != "1") code = "protocol_mismatch";
        else if (!Guid.TryParse(candidate, out profile)) code = "invalid_profile_identity";
        else if (File.Exists(pairFile)) {
            var pair = Serializer().DeserializeObject(File.ReadAllText(pairFile)) as Dictionary<string, object>;
            if (Text(pair, "profile_id") != candidate) code = "paired_profile_mismatch";
        }
        if (code != "") { Send(new Dictionary<string, object> { { "type", "hello_rejected" }, { "code", code } }); return false; }
        if (!File.Exists(pairFile)) AtomicJson(pairFile, new Dictionary<string, object> { { "profile_id", candidate } });
        ProfileId = candidate; ExtensionVersion = Text(message, "extension_version"); Connected = true;
        WriteState(); Send(new Dictionary<string, object> { { "type", "hello_ack" }, { "protocol", Protocol }, { "generation", Generation }, { "host_version", "0.2.0" } });
        return true;
    }
    private static bool Cancel(string id, string code)
    {
        PendingRequest p; if (!Pending.TryGetValue(id, out p)) return false;
        lock (p) {
            if (!p.Finish(Failure(code, id, true))) return false;
            try { Send(new Dictionary<string, object> { { "type", "cancel" }, { "request_id", id }, { "generation", Generation } }); } catch { }
            return true;
        }
    }
    private static void ReadLoop()
    {
        try {
            var first = ReadMessage(); if (first == null || Text(first, "type") != "hello" || !Hello(first)) return;
            while (true) {
                var message = ReadMessage(); if (message == null) break;
                if (Text(message, "type") != "result" || Text(message, "generation") != Generation) continue;
                PendingRequest p; if (Pending.TryGetValue(Text(message, "request_id"), out p) && Text(message, "platform") == p.Platform) p.Finish(message);
            }
        } catch (Exception ex) { Console.Error.WriteLine("native: " + ex.GetType().Name); }
        finally {
            Connected = false; WriteState();
            foreach (var pair in Pending) pair.Value.Finish(Failure("browser_disconnected", pair.Key, true));
            Thread.Sleep(150); try { Listener.Stop(); } catch { }
        }
    }
    private static Dictionary<string, object> ReadBody(HttpListenerContext context)
    {
        if (context.Request.ContentLength64 > 1024 * 1024) throw new InvalidDataException("request_too_large");
        using (var reader = new StreamReader(context.Request.InputStream, Encoding.UTF8)) {
            char[] buffer = new char[1024 * 1024 + 1]; int count = 0, n;
            while ((n = reader.Read(buffer, count, buffer.Length - count)) > 0) { count += n; if (count > 1024 * 1024) throw new InvalidDataException("request_too_large"); }
            return Serializer().DeserializeObject(new string(buffer, 0, count)) as Dictionary<string, object> ?? new Dictionary<string, object>();
        }
    }
    private static void Reply(HttpListenerContext context, int status, object value)
    { byte[] bytes = Encoding.UTF8.GetBytes(Serializer().Serialize(value)); context.Response.StatusCode = status; context.Response.ContentType = "application/json"; context.Response.ContentLength64 = bytes.Length; context.Response.OutputStream.Write(bytes, 0, bytes.Length); context.Response.Close(); }
    private static void Handle(HttpListenerContext context)
    {
        try {
            if (context.Request.Headers["X-KR-Native-Token"] != Token || context.Request.Headers["Origin"] != null) { context.Response.StatusCode = 401; context.Response.Close(); return; }
            string path = context.Request.Url.AbsolutePath;
            if (path == "/health" && context.Request.HttpMethod == "GET") {
                Reply(context, 200, new Dictionary<string, object> { { "ok", Connected }, { "connected", Connected }, { "protocol", Protocol }, { "generation", Generation }, { "profile_id", ProfileId }, { "pending", Pending.Count }, { "draining", Draining }, { "extension_version", ExtensionVersion } }); return;
            }
            if (context.Request.HttpMethod != "POST") { context.Response.StatusCode = 405; context.Response.Close(); return; }
            if (path == "/drain") { lock (AdmissionLock) { Draining = true; Send(new Dictionary<string, object> { { "type", "drain" } }); Reply(context, 200, new Dictionary<string, object> { { "ok", true }, { "pending", Pending.Count } }); } return; }
            if (path == "/resume") { lock (AdmissionLock) { Draining = false; Send(new Dictionary<string, object> { { "type", "resume" } }); Reply(context, 200, new Dictionary<string, object> { { "ok", true } }); } return; }
            if (path == "/shutdown") {
                if (!Draining || Pending.Count != 0) { Reply(context, 409, Failure("channel_not_drained", "", false)); return; }
                Reply(context, 200, new Dictionary<string, object> { { "ok", true } }); Connected = false; WriteState(); Listener.Stop(); return;
            }
            if (path == "/cancel" || path == "/cancel_task") {
                var body = ReadBody(context); string id = Text(body, "request_id"), task = Text(body, "task_id"); int count = 0;
                if (path == "/cancel") { Seen[id] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(); if (Cancel(id, "cancelled")) count++; }
                else if (task != "") { CancelledTasks[task] = DateTimeOffset.UtcNow.ToUnixTimeSeconds(); foreach (var pair in Pending) if (pair.Value.TaskId == task && Cancel(pair.Key, "cancelled")) count++; }
                Reply(context, 200, new Dictionary<string, object> { { "ok", true }, { "cancelled", count } }); return;
            }
            if (path != "/command") { context.Response.StatusCode = 404; context.Response.Close(); return; }
            var command = ReadBody(context); string requestId = Text(command, "request_id"), platform = Text(command, "platform"), operation = Text(command, "operation");
            if (!Connected) { Reply(context, 409, Failure("browser_disconnected", requestId, false)); return; }
            if (Draining) { Reply(context, 409, Failure("channel_draining", requestId, false)); return; }
            if (Text(command, "generation") != Generation) { Reply(context, 409, Failure("stale_generation", requestId, false)); return; }
            if (!Regex.IsMatch(platform, "^[a-z][a-z0-9_]{0,31}$") || !Operations.Contains(operation)) { Reply(context, 400, Failure("unsupported_operation", requestId, false)); return; }
            var payload = command.ContainsKey("payload") ? command["payload"] as Dictionary<string, object> : null;
            var request = new PendingRequest { TaskId = Text(payload, "task_id"), Platform = platform };
            lock (AdmissionLock) {
                if (!Connected || Draining) { Reply(context, 409, Failure(Draining ? "channel_draining" : "browser_disconnected", requestId, false)); return; }
                long now = DateTimeOffset.UtcNow.ToUnixTimeSeconds(), removed;
                foreach (var pair in Seen) if (now - pair.Value > 600) Seen.TryRemove(pair.Key, out removed);
                foreach (var pair in CancelledTasks) if (now - pair.Value > 600) CancelledTasks.TryRemove(pair.Key, out removed);
                if (requestId == "" || Seen.ContainsKey(requestId)) { Reply(context, 409, Failure("duplicate_request", requestId, false)); return; }
                if (Pending.Count >= 2) { Reply(context, 409, Failure("browser_busy", requestId, false)); return; }
                foreach (var pair in Pending) if (pair.Value.Platform == platform) { Reply(context, 409, Failure("browser_busy", requestId, false)); return; }
                if (request.TaskId != "" && CancelledTasks.ContainsKey(request.TaskId)) { Reply(context, 409, Failure("cancelled", requestId, false)); return; }
                Seen[requestId] = now; Pending[requestId] = request;
            }
            try {
                int timeout = 20000, parsed; if (Int32.TryParse(Text(command, "timeout_ms"), out parsed)) timeout = Math.Max(1, Math.Min(20000, parsed));
                lock (request) {
                    if (request.TaskId != "" && CancelledTasks.ContainsKey(request.TaskId)) request.Finish(Failure("cancelled", requestId, false));
                    if (request.Result == null) Send(new Dictionary<string, object> { { "type", "command" }, { "request_id", requestId }, { "platform", platform }, { "operation", operation }, { "payload", payload }, { "generation", Generation }, { "timeout_ms", timeout } });
                }
                if (!request.Done.Wait(timeout + 300)) Cancel(requestId, "native_command_timeout");
                var result = request.Result ?? Failure("native_command_timeout", requestId, true);
                bool ok = result.ContainsKey("ok") && Convert.ToBoolean(result["ok"]);
                Reply(context, ok ? 200 : 502, result);
            } finally { PendingRequest removed; Pending.TryRemove(requestId, out removed); }
        } catch (Exception ex) { Console.Error.WriteLine("http: " + ex.GetType().Name); try { Reply(context, 400, Failure("native_host_invalid_request", "", false)); } catch { } }
    }
    public static void Main()
    {
        string scope = Environment.GetEnvironmentVariable("KR_BROWSER_LINK_TEST_SCOPE") ?? WindowsIdentity.GetCurrent().User.Value;
        bool created;
        using (var owner = new Mutex(true, "Local\\KnowledgeRadar.BrowserLink." + scope, out created)) {
            if (!created) { Send(new Dictionary<string, object> { { "type", "hello_rejected" }, { "code", "channel_already_owned" } }); return; }
            try {
                for (int port = 19890; port <= 19949; port++) {
                    try { Listener = new HttpListener(); Listener.Prefixes.Add("http://127.0.0.1:" + port + "/"); Listener.Start(); Port = port; break; }
                    catch { try { Listener.Close(); } catch { } }
                }
                if (Port == 0) return;
                new Thread(ReadLoop) { IsBackground = true }.Start();
                while (Listener.IsListening) { try { var context = Listener.GetContext(); ThreadPool.QueueUserWorkItem(_ => Handle(context)); } catch { break; } }
            } finally { Connected = false; WriteState(); try { if (Listener != null) Listener.Close(); } catch { } owner.ReleaseMutex(); }
        }
    }
}
