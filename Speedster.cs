// Speedster - periodic internet speed test in the Windows tray.
// Runs a download/upload/latency test on a schedule, logs every result to plain
// text under %LOCALAPPDATA%\Speedster, and renders a self-contained HTML report.
// Tray app, unelevated, .NET Framework 4.x.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

namespace Speedster
{
    static class Config
    {
        public const string APP_NAME = "Speedster";
        public const string CF_HOST = "https://speed.cloudflare.com";
        public const int TICK_MS = 30000;          // scheduler resolution
    }

    static class Native
    {
        [DllImport("user32.dll")]
        public static extern bool DestroyIcon(IntPtr handle);
    }

    // ---- paths (everything plain text, on-device) ----
    static class Paths
    {
        public static string Dir
        {
            get
            {
                string d = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), Config.APP_NAME);
                if (!Directory.Exists(d)) Directory.CreateDirectory(d);
                return d;
            }
        }

        public static string Settings { get { return Path.Combine(Dir, "settings.ini"); } }
        public static string Results { get { return Path.Combine(Dir, "results.csv"); } }
        public static string Log { get { return Path.Combine(Dir, "speedster.log"); } }
        public static string Report { get { return Path.Combine(Dir, "report.html"); } }
    }

    static class Diag
    {
        static readonly object _gate = new object();

        // Rolling diagnostics file, truncated so it never grows without bound.
        public static void Write(string msg)
        {
            try
            {
                lock (_gate)
                {
                    var fi = new FileInfo(Paths.Log);
                    if (fi.Exists && fi.Length > 256 * 1024) fi.Delete();
                    File.AppendAllText(Paths.Log, DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + "  " + msg + Environment.NewLine);
                }
            }
            catch { /* diagnostics must never break the app */ }
        }
    }

    // ---- settings.ini (key=value, '#' comments, unknown keys preserved) ----
    class Settings
    {
        public int IntervalMinutes = 60;
        public bool Paused = false;
        public string Engine = "cloudflare";       // cloudflare | ookla
        public string OoklaPath = "";
        public bool SkipMetered = true;
        public List<string> OnlyNetworks = new List<string>();

        // Measurement budget. A transfer stops at whichever comes first: target_seconds of
        // steady state, or max_bytes. Time is what makes the number valid; bytes are the cost
        // ceiling. Defaults land a full test under ~5 MB on a slow link, under 9 MB on a fast one.
        public double TargetSecondsDown = 3;
        public double TargetSecondsUp = 3;
        public long MaxBytesDown = 6000000;
        public long MaxBytesUp = 3000000;
        public int Streams = 4;
        public int MaxTestSeconds = 20;             // absolute per-direction guard against stalls

        // Estimator. The opening of a transfer is TCP slow start and reads low, so it is dropped
        // before averaging - unless the whole window is too short to afford it.
        public int DiscardMs = 500;
        public int DiscardPercent = 25;
        public int MinWindowMs = 1200;
        public int SampleMs = 100;                  // throughput sampling resolution
        public int LatencySamples = 12;

        // I/O granularity. The stop condition is only tested between reads/writes, so a smaller
        // chunk means the transfer stops closer to target_seconds - at the cost of more syscalls.
        public int ReadBufferBytes = 65536;
        public int WriteChunkBytes = 65536;

        // Ceiling on a single HTTP request. A stream needing more issues successive requests.
        public long RequestBytesMax = 25000000;

        // speed.cloudflare.com throttles by recent volume, answering 403/429 once a client has
        // pulled a lot in a short window. A retry at half the size usually gets through.
        public int RetryCount = 2;
        public int RetryDelayMs = 1000;

        public int StartupDelaySeconds = 60;
        public DateTime LastRun = DateTime.MinValue;   // UTC; MinValue = never

        readonly Dictionary<string, string> _extra = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        public static Settings Load()
        {
            var s = new Settings();
            try
            {
                if (!File.Exists(Paths.Settings)) { s.Save(); return s; }
                foreach (string raw in File.ReadAllLines(Paths.Settings))
                {
                    string line = raw.Trim();
                    if (line.Length == 0 || line[0] == '#' || line[0] == ';') continue;
                    int eq = line.IndexOf('=');
                    if (eq < 1) continue;
                    string k = line.Substring(0, eq).Trim();
                    string v = line.Substring(eq + 1).Trim();
                    int hash = v.IndexOf('#');
                    if (hash >= 0) v = v.Substring(0, hash).Trim();   // tolerate a trailing comment
                    s.Set(k, v);
                }
            }
            catch (Exception ex) { Diag.Write("settings load failed: " + ex.Message); }
            s.Clamp();
            return s;
        }

        void Set(string k, string v)
        {
            switch (k.ToLowerInvariant())
            {
                case "interval_minutes": IntervalMinutes = ParseInt(v, IntervalMinutes); break;
                case "paused": Paused = ParseBool(v, Paused); break;
                case "engine": Engine = v.Length > 0 ? v.ToLowerInvariant() : "cloudflare"; break;
                case "ookla_path": OoklaPath = v; break;
                case "skip_metered": SkipMetered = ParseBool(v, SkipMetered); break;
                case "only_networks": OnlyNetworks = SplitList(v); break;
                case "target_seconds_down": TargetSecondsDown = ParseDouble(v, TargetSecondsDown); break;
                case "target_seconds_up": TargetSecondsUp = ParseDouble(v, TargetSecondsUp); break;
                case "max_bytes_down": MaxBytesDown = ParseLong(v, MaxBytesDown); break;
                case "max_bytes_up": MaxBytesUp = ParseLong(v, MaxBytesUp); break;
                case "target_bytes_down": MaxBytesDown = ParseLong(v, MaxBytesDown); break;   // pre-time-budget name
                case "target_bytes_up": MaxBytesUp = ParseLong(v, MaxBytesUp); break;         // pre-time-budget name
                case "streams": Streams = ParseInt(v, Streams); break;
                case "max_test_seconds": MaxTestSeconds = ParseInt(v, MaxTestSeconds); break;
                case "discard_ms": DiscardMs = ParseInt(v, DiscardMs); break;
                case "discard_percent": DiscardPercent = ParseInt(v, DiscardPercent); break;
                case "min_window_ms": MinWindowMs = ParseInt(v, MinWindowMs); break;
                case "sample_ms": SampleMs = ParseInt(v, SampleMs); break;
                case "latency_samples": LatencySamples = ParseInt(v, LatencySamples); break;
                case "read_buffer_bytes": ReadBufferBytes = ParseInt(v, ReadBufferBytes); break;
                case "write_chunk_bytes": WriteChunkBytes = ParseInt(v, WriteChunkBytes); break;
                case "request_bytes_max": RequestBytesMax = ParseLong(v, RequestBytesMax); break;
                case "retry_count": RetryCount = ParseInt(v, RetryCount); break;
                case "retry_delay_ms": RetryDelayMs = ParseInt(v, RetryDelayMs); break;
                case "run_on_startup_delay_seconds": StartupDelaySeconds = ParseInt(v, StartupDelaySeconds); break;
                case "last_run": LastRun = ParseTime(v); break;
                default: _extra[k] = v; break;
            }
        }

        // Ranges are deliberately wide - the bounds exist to stop a typo producing a hung or
        // runaway test, not to second-guess a deliberate choice.
        void Clamp()
        {
            IntervalMinutes = Bound(IntervalMinutes, 1, 525600);              // 1 min .. 1 year
            Streams = Bound(Streams, 1, 64);
            MaxTestSeconds = Bound(MaxTestSeconds, 1, 3600);
            TargetSecondsDown = Bound(TargetSecondsDown, 0.1, 3600);
            TargetSecondsUp = Bound(TargetSecondsUp, 0.1, 3600);
            MaxBytesDown = Bound(MaxBytesDown, 50000L, 100000000000L);        // 50 KB .. 100 GB
            MaxBytesUp = Bound(MaxBytesUp, 50000L, 100000000000L);
            DiscardMs = Bound(DiscardMs, 0, 600000);
            DiscardPercent = Bound(DiscardPercent, 0, 90);
            MinWindowMs = Bound(MinWindowMs, 0, 3600000);
            SampleMs = Bound(SampleMs, 10, 60000);
            LatencySamples = Bound(LatencySamples, 1, 1000);
            ReadBufferBytes = Bound(ReadBufferBytes, 1024, 8388608);
            WriteChunkBytes = Bound(WriteChunkBytes, 1024, 8388608);
            RequestBytesMax = Bound(RequestBytesMax, 100000L, 90000000L);
            RetryCount = Bound(RetryCount, 0, 10);
            RetryDelayMs = Bound(RetryDelayMs, 0, 60000);
            StartupDelaySeconds = Bound(StartupDelaySeconds, 0, 86400);
            if (Engine != "ookla") Engine = "cloudflare";
        }

        static int Bound(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
        static long Bound(long v, long lo, long hi) { return v < lo ? lo : (v > hi ? hi : v); }
        static double Bound(double v, double lo, double hi) { return v < lo ? lo : (v > hi ? hi : v); }

        public void Save()
        {
            try
            {
                var sb = new StringBuilder();
                sb.AppendLine("# Speedster settings - plain text, edit freely. Comments must be on their own line.");
                sb.AppendLine();
                sb.AppendLine("# How often to run a test, in minutes.");
                sb.AppendLine("interval_minutes=" + IntervalMinutes.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("paused=" + (Paused ? "true" : "false"));
                sb.AppendLine();
                sb.AppendLine("# cloudflare = built-in HTTP test; ookla = the speedtest.exe named by ookla_path.");
                sb.AppendLine("engine=" + Engine);
                sb.AppendLine("ookla_path=" + OoklaPath);
                sb.AppendLine();
                sb.AppendLine("# skip_metered: never test on a connection Windows reports as metered.");
                sb.AppendLine("# only_networks: comma-separated network names (Wi-Fi SSIDs); empty = any network.");
                sb.AppendLine("skip_metered=" + (SkipMetered ? "true" : "false"));
                sb.AppendLine("only_networks=" + string.Join(",", OnlyNetworks.ToArray()));
                sb.AppendLine();
                sb.AppendLine("# Measurement budget. Each direction stops at whichever comes first:");
                sb.AppendLine("#   target_seconds_* of transfer, or max_bytes_* moved.");
                sb.AppendLine("# Seconds are what make the reading valid; bytes are the data-cost ceiling.");
                sb.AppendLine("# Raise target_seconds on a noisy link; raise max_bytes on a fast one.");
                sb.AppendLine("target_seconds_down=" + TargetSecondsDown.ToString("0.###", CultureInfo.InvariantCulture));
                sb.AppendLine("target_seconds_up=" + TargetSecondsUp.ToString("0.###", CultureInfo.InvariantCulture));
                sb.AppendLine("max_bytes_down=" + MaxBytesDown.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("max_bytes_up=" + MaxBytesUp.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("streams=" + Streams.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# max_test_seconds is an absolute per-direction guard against a stalled transfer.");
                sb.AppendLine("max_test_seconds=" + MaxTestSeconds.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# Estimator. The start of a transfer is TCP slow start and reads low, so the first");
                sb.AppendLine("# max(discard_ms, discard_percent of the window) is dropped before averaging -");
                sb.AppendLine("# unless the whole window came in under min_window_ms, where there is nothing to spare.");
                sb.AppendLine("# sample_ms is the throughput sampling resolution.");
                sb.AppendLine("discard_ms=" + DiscardMs.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("discard_percent=" + DiscardPercent.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("min_window_ms=" + MinWindowMs.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("sample_ms=" + SampleMs.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# Round trips timed per test. The first is a warm-up and is not counted.");
                sb.AppendLine("latency_samples=" + LatencySamples.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# I/O granularity. The stop condition is checked between reads/writes, so smaller");
                sb.AppendLine("# chunks stop nearer to target_seconds at the cost of more syscalls.");
                sb.AppendLine("read_buffer_bytes=" + ReadBufferBytes.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("write_chunk_bytes=" + WriteChunkBytes.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# Ceiling on one HTTP request; a stream needing more issues successive requests.");
                sb.AppendLine("# The test server throttles by recent volume (403/429); a throttled request is");
                sb.AppendLine("# retried retry_count times, halving the size each time.");
                sb.AppendLine("request_bytes_max=" + RequestBytesMax.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("retry_count=" + RetryCount.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine("retry_delay_ms=" + RetryDelayMs.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# Grace period after login before the first test.");
                sb.AppendLine("run_on_startup_delay_seconds=" + StartupDelaySeconds.ToString(CultureInfo.InvariantCulture));
                sb.AppendLine();
                sb.AppendLine("# Written by Speedster after every run.");
                sb.AppendLine("last_run=" + (LastRun == DateTime.MinValue ? "" : LastRun.ToString("o", CultureInfo.InvariantCulture)));
                foreach (var kv in _extra) sb.AppendLine(kv.Key + "=" + kv.Value);
                File.WriteAllText(Paths.Settings, sb.ToString());
            }
            catch (Exception ex) { Diag.Write("settings save failed: " + ex.Message); }
        }

        public bool NetworkAllowed(string name)
        {
            if (OnlyNetworks.Count == 0) return true;
            foreach (string n in OnlyNetworks)
                if (string.Equals(n, name, StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        public static List<string> SplitList(string v)
        {
            var list = new List<string>();
            foreach (string p in v.Split(','))
            {
                string t = p.Trim();
                if (t.Length > 0 && !list.Contains(t)) list.Add(t);
            }
            return list;
        }

        static int ParseInt(string v, int fallback) { int n; return int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) ? n : fallback; }
        static double ParseDouble(string v, double fallback) { double d; return double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d) ? d : fallback; }
        static long ParseLong(string v, long fallback) { long n; return long.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) ? n : fallback; }
        static bool ParseBool(string v, bool fallback)
        {
            if (v.Length == 0) return fallback;
            v = v.ToLowerInvariant();
            return v == "1" || v == "true" || v == "yes" || v == "on";
        }
        static DateTime ParseTime(string v)
        {
            DateTime d;
            if (DateTime.TryParse(v, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out d)) return d;
            return DateTime.MinValue;
        }
    }

    // ---- current network: name (SSID for Wi-Fi) from Network List Manager, metered cost from WinRT ----
    static class Net
    {
        const uint NLM_ENUM_NETWORK_CONNECTED = 0x1;

        static readonly Guid CLSID_NetworkListManager = new Guid("DCB00C01-570F-4A9B-8D69-199FDBA5723B");

        [ComImport, Guid("DCB00000-570F-4A9B-8D69-199FDBA5723B"), InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
        interface INetworkListManager
        {
            object GetNetworks(uint flags);
            object GetNetwork(Guid networkId);
            object GetNetworkConnections();
            object GetNetworkConnection(Guid connectionId);
            bool IsConnectedToInternet { get; }
            bool IsConnected { get; }
        }

        [ComImport, Guid("DCB00002-570F-4A9B-8D69-199FDBA5723B"), InterfaceType(ComInterfaceType.InterfaceIsIDispatch)]
        interface INetwork
        {
            string GetName();
            bool IsConnectedToInternet { get; }
            bool IsConnected { get; }
        }

        public struct Info
        {
            public string Name;
            public bool HasInternet;
            public string CostType;        // Unknown | Unrestricted | Fixed | Variable
            public bool Metered;           // Fixed/Variable cost, over data limit, or roaming
            public bool OverLimit;
            public bool ApproachingLimit;
        }

        public static Info Current()
        {
            var info = new Info();
            info.Name = "";
            info.CostType = "Unknown";
            object nlm = null;
            try
            {
                Type t = Type.GetTypeFromCLSID(CLSID_NetworkListManager);
                nlm = Activator.CreateInstance(t);
                var mgr = (INetworkListManager)nlm;
                info.HasInternet = mgr.IsConnectedToInternet;

                // First connected network that has internet; else first connected network.
                var connected = mgr.GetNetworks(NLM_ENUM_NETWORK_CONNECTED) as System.Collections.IEnumerable;
                if (connected != null)
                {
                    string fallbackName = "";
                    foreach (object o in connected)
                    {
                        var n = o as INetwork;
                        if (n == null) continue;
                        string name = n.GetName();
                        if (string.IsNullOrEmpty(name)) continue;
                        if (n.IsConnectedToInternet) { info.Name = name; break; }
                        if (fallbackName.Length == 0) fallbackName = name;
                    }
                    if (info.Name.Length == 0) info.Name = fallbackName;
                }
            }
            catch (Exception ex)
            {
                Diag.Write("NLM query failed: " + ex.Message);
            }
            finally
            {
                if (nlm != null && Marshal.IsComObject(nlm)) Marshal.ReleaseComObject(nlm);
            }

            Cost(ref info);
            if (info.Name.Length == 0) info.Name = FallbackName(ref info.HasInternet);
            if (info.Name.Length == 0) info.Name = "unknown";
            return info;
        }

        // Metered state comes from WinRT ConnectionCost. The Network List Manager's
        // INetworkCostManager is not obtainable from the NetworkListManager coclass
        // (E_NOINTERFACE on Windows 11), and csc cannot consume .winmd, so the WinRT
        // projection is reached by reflection - available on .NET Framework 4.5+.
        static void Cost(ref Info info)
        {
            try
            {
                Type statics = Type.GetType("Windows.Networking.Connectivity.NetworkInformation, Windows.Networking, ContentType=WindowsRuntime");
                if (statics == null) return;
                MethodInfo get = statics.GetMethod("GetInternetConnectionProfile", BindingFlags.Public | BindingFlags.Static);
                if (get == null) return;
                object profile = get.Invoke(null, null);
                if (profile == null) return;

                if (info.Name.Length == 0)
                {
                    object pn = profile.GetType().GetProperty("ProfileName").GetValue(profile, null);
                    if (pn != null) info.Name = pn.ToString();
                }
                info.HasInternet = true;

                object cost = profile.GetType().GetMethod("GetConnectionCost").Invoke(profile, null);
                if (cost == null) return;
                Type ct = cost.GetType();
                int type = (int)ct.GetProperty("NetworkCostType").GetValue(cost, null);   // 0 unknown, 1 unrestricted, 2 fixed, 3 variable
                info.OverLimit = (bool)ct.GetProperty("OverDataLimit").GetValue(cost, null);
                info.ApproachingLimit = (bool)ct.GetProperty("ApproachingDataLimit").GetValue(cost, null);
                bool roaming = (bool)ct.GetProperty("Roaming").GetValue(cost, null);

                info.CostType = type == 1 ? "Unrestricted" : type == 2 ? "Fixed" : type == 3 ? "Variable" : "Unknown";
                info.Metered = type == 2 || type == 3 || info.OverLimit || roaming;
            }
            catch (Exception ex)
            {
                Diag.Write("connection cost query failed: " + ex.Message);
            }
        }

        // Managed fallback when the COM path is unavailable: first up interface with a gateway.
        static string FallbackName(ref bool hasInternet)
        {
            try
            {
                foreach (NetworkInterface ni in NetworkInterface.GetAllNetworkInterfaces())
                {
                    if (ni.OperationalStatus != OperationalStatus.Up) continue;
                    if (ni.NetworkInterfaceType == NetworkInterfaceType.Loopback || ni.NetworkInterfaceType == NetworkInterfaceType.Tunnel) continue;
                    var props = ni.GetIPProperties();
                    if (props.GatewayAddresses.Count == 0) continue;
                    hasInternet = true;
                    return ni.Name;
                }
            }
            catch (Exception ex) { Diag.Write("interface fallback failed: " + ex.Message); }
            return "";
        }
    }

    // ---- one row of results.csv ----
    class Result
    {
        public DateTime TimestampUtc = DateTime.UtcNow;
        public double DownMbps, UpMbps, LatencyMs, JitterMs;
        public double DownSeconds, UpSeconds;      // length of the window each rate was measured over
        public long DownBytes, UpBytes;
        public string Network = "";
        public bool Metered;
        public string Engine = "";
        public string Server = "";
        public string Error = "";

        public bool Ok { get { return Error.Length == 0; } }
    }

    static class SpeedTest
    {
        static readonly Regex RX_DUR = new Regex(@"dur\s*=\s*([\d.]+)", RegexOptions.IgnoreCase);

        static void Prepare()
        {
            try { ServicePointManager.SecurityProtocol |= (SecurityProtocolType)(768 | 3072); } catch { } // Tls11 | Tls12
            ServicePointManager.DefaultConnectionLimit = 64;
            ServicePointManager.Expect100Continue = false;
            ServicePointManager.UseNagleAlgorithm = false;
        }

        public static Result Run(Settings s, Net.Info net)
        {
            var r = new Result();
            r.Network = net.Name;
            r.Metered = net.Metered;
            r.Engine = s.Engine;
            try
            {
                Prepare();
                if (s.Engine == "ookla") RunOokla(s, r);
                else RunCloudflare(s, r);
            }
            catch (Exception ex)
            {
                r.Error = ex.GetType().Name + ": " + ex.Message;
                Diag.Write("test failed: " + ex);
            }
            return r;
        }

        // ---- built-in engine: speed.cloudflare.com ----
        static void RunCloudflare(Settings s, Result r)
        {
            string server;
            Latency(s, out r.LatencyMs, out r.JitterMs, out server);
            r.Server = server;
            // The latency phase leaves a warm, keep-alive connection in the pool, so the
            // transfers below start with a grown congestion window rather than from cold.

            double mbps, secs;
            string downError, upError;
            r.DownBytes = Transfer(false, s, out secs, out mbps, out downError);
            r.DownMbps = mbps; r.DownSeconds = secs;

            r.UpBytes = Transfer(true, s, out secs, out mbps, out upError);
            r.UpMbps = mbps; r.UpSeconds = secs;

            // A direction that moved nothing is a failure, not a 0 Mbps reading.
            if (downError != null && upError != null) r.Error = "download and upload failed: " + downError;
            else if (downError != null) r.Error = "download failed: " + downError;
            else if (upError != null) r.Error = "upload failed: " + upError;
        }

        // Round-trip time to a zero-byte response, minus Cloudflare's own processing time.
        static void Latency(Settings s, out double minMs, out double jitterMs, out string server)
        {
            minMs = 0; jitterMs = 0; server = "";
            var samples = new List<double>();
            // i == 0 is a warm-up: it pays for DNS + TCP + TLS and would skew both min and jitter.
            for (int i = 0; i <= s.LatencySamples; i++)
            {
                try
                {
                    var req = NewRequest(Config.CF_HOST + "/__down?bytes=0");
                    var sw = Stopwatch.StartNew();
                    using (var resp = (HttpWebResponse)req.GetResponse())
                    {
                        using (var st = resp.GetResponseStream())
                        {
                            var buf = new byte[64];
                            while (st.Read(buf, 0, buf.Length) > 0) { }
                        }
                        sw.Stop();
                        double rtt = sw.Elapsed.TotalMilliseconds - ServerTime(resp);
                        if (rtt > 0 && i > 0) samples.Add(rtt);
                        if (server.Length == 0) server = ServerName(resp);
                    }
                }
                catch (Exception ex)
                {
                    if (i == 0) Diag.Write("latency sample failed: " + ex.Message);
                }
            }
            if (samples.Count == 0) return;

            minMs = double.MaxValue;
            foreach (double v in samples) if (v < minMs) minMs = v;
            double sum = 0;
            for (int i = 1; i < samples.Count; i++) sum += Math.Abs(samples[i] - samples[i - 1]);
            if (samples.Count > 1) jitterMs = sum / (samples.Count - 1);
        }

        // Cloudflare reports its own processing time as Server-Timing metrics (cfSpeedEdge,
        // cfSpeedWorker). Their sum subtracted from the round trip lands within ~1 ms of the
        // TCP min_rtt the edge reports, so sum every dur= rather than taking the first.
        static double ServerTime(HttpWebResponse resp)
        {
            try
            {
                string h = resp.Headers["Server-Timing"];
                if (string.IsNullOrEmpty(h)) return 0;
                double total = 0;
                foreach (Match m in RX_DUR.Matches(h))
                {
                    double d;
                    if (double.TryParse(m.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out d)) total += d;
                }
                return total;
            }
            catch { }
            return 0;
        }

        static string ServerName(HttpWebResponse resp)
        {
            string colo = resp.Headers["colo"];
            string city = resp.Headers["city"];
            if (!string.IsNullOrEmpty(colo) && !string.IsNullOrEmpty(city)) return city + " (" + colo + ")";
            if (!string.IsNullOrEmpty(colo)) return colo;
            return city == null ? "" : city;
        }

        static HttpWebRequest NewRequest(string url)
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Proxy = null;                 // skip proxy autodetect, it distorts timings
            req.KeepAlive = true;
            req.Timeout = 30000;
            req.ReadWriteTimeout = 30000;
            req.AllowAutoRedirect = false;
            req.CachePolicy = new System.Net.Cache.RequestCachePolicy(System.Net.Cache.RequestCacheLevel.NoCacheNoStore);
            req.UserAgent = Config.APP_NAME;
            return req;
        }

        // Tracks bytes over time for one direction: total moved, when the wire went live, and a
        // cumulative-bytes sample every sample_ms so a rate can be computed over any sub-window.
        class Meter
        {
            public readonly Stopwatch Sw = Stopwatch.StartNew();
            readonly object _gate = new object();
            readonly List<long[]> _samples = new List<long[]>();   // [elapsedMs, cumulativeBytes]
            long _bytes;
            long _firstTick = -1;
            long _lastTick;

            public long Bytes { get { return Interlocked.Read(ref _bytes); } }
            public long FirstTick { get { return Interlocked.Read(ref _firstTick); } }
            public string Error { get; private set; }

            // First failure wins - it is the one worth reporting.
            public void Fail(string msg) { lock (_gate) if (Error == null) Error = msg; }

            public void Add(int n) { Interlocked.Add(ref _bytes, n); }
            public void MarkFirstByte() { Interlocked.CompareExchange(ref _firstTick, Sw.ElapsedMilliseconds, -1); }
            public void MarkEnd() { lock (_gate) { long t = Sw.ElapsedMilliseconds; if (t > _lastTick) _lastTick = t; } }
            public void Sample() { lock (_gate) _samples.Add(new long[] { Sw.ElapsedMilliseconds, Bytes }); }

            // Stop once the target window has elapsed since the first byte, or the hard guard trips.
            public bool Done(long targetMs, long hardMs)
            {
                long t = Sw.ElapsedMilliseconds;
                if (t >= hardMs) return true;
                long f = FirstTick;
                return f >= 0 && t - f >= targetMs;
            }

            long BytesAt(long ms)
            {
                long b = 0;
                lock (_gate)
                    foreach (long[] s in _samples)
                        if (s[0] <= ms) b = s[1]; else break;
                return b;
            }

            // Mean rate over the window left after dropping the slow-start opening.
            public void Estimate(Settings cfg, out double seconds, out double mbps)
            {
                seconds = 0; mbps = 0;
                long first = FirstTick < 0 ? 0 : FirstTick;
                long last;
                lock (_gate) last = _lastTick;
                long span = last - first;
                if (span <= 0) return;

                long cut = first;
                if (span >= cfg.MinWindowMs)
                {
                    long drop = Math.Max(cfg.DiscardMs, (long)(span * cfg.DiscardPercent / 100.0));
                    if (drop < span) cut = first + drop;
                }

                long bytes = Bytes - BytesAt(cut);
                double secs = (last - cut) / 1000.0;
                if (bytes <= 0 || secs <= 0)     // nothing left after the cut - use the whole window
                {
                    bytes = Bytes;
                    secs = span / 1000.0;
                    cut = first;
                }
                if (bytes <= 0 || secs <= 0) return;
                seconds = secs;
                mbps = bytes * 8.0 / secs / 1e6;
            }
        }

        // Runs `streams` concurrent transfers, stopping at target_seconds or max_bytes - whichever
        // arrives first. Returns every byte actually moved; `seconds`/`mbps` describe the window the
        // rate was measured over, which is shorter than the transfer by the discarded ramp-up.
        static long Transfer(bool upload, Settings cfg, out double seconds, out double mbps, out string error)
        {
            long maxBytes = upload ? cfg.MaxBytesUp : cfg.MaxBytesDown;
            long targetMs = (long)((upload ? cfg.TargetSecondsUp : cfg.TargetSecondsDown) * 1000.0);
            long hardMs = cfg.MaxTestSeconds * 1000L;
            long perStream = Math.Max(1, maxBytes / cfg.Streams);
            var m = new Meter();

            bool sampling = true;
            var sampler = new Thread(delegate()
            {
                while (sampling) { Thread.Sleep(cfg.SampleMs); m.Sample(); }
            });
            sampler.IsBackground = true;
            sampler.Start();

            var threads = new Thread[cfg.Streams];
            for (int i = 0; i < cfg.Streams; i++)
            {
                threads[i] = new Thread(delegate()
                {
                    try { if (upload) Up(m, cfg, perStream, targetMs, hardMs); else Down(m, cfg, perStream, targetMs, hardMs); }
                    catch (Exception ex)
                    {
                        m.Fail(ex.Message);
                        Diag.Write((upload ? "upload" : "download") + " stream failed: " + ex.Message);
                    }
                    m.MarkEnd();
                });
                threads[i].IsBackground = true;
            }
            foreach (var t in threads) t.Start();
            foreach (var t in threads) t.Join((int)Math.Min(hardMs * 3, 600000));

            sampling = false;
            m.Sample();
            m.Estimate(cfg, out seconds, out mbps);
            error = m.Bytes > 0 ? null : (m.Error ?? "no bytes transferred");
            return m.Bytes;
        }

        // Was this request refused for asking too much too soon, rather than genuinely broken?
        static bool Throttled(WebException ex)
        {
            var resp = ex.Response as HttpWebResponse;
            if (resp == null) return false;
            int code = (int)resp.StatusCode;
            return code == 403 || code == 429 || code == 503;
        }

        // One stream's share, as however many back-to-back requests it takes. A throttled request
        // is retried at half the size, which also shrinks what the next attempt asks for.
        static void Down(Meter m, Settings cfg, long bytes, long targetMs, long hardMs)
        {
            long left = bytes;
            while (left > 0 && !m.Done(targetMs, hardMs))
            {
                long ask = Math.Min(left, cfg.RequestBytesMax);
                left -= ask;
                for (int attempt = 0; ; attempt++)
                {
                    try { DownOnce(m, cfg, ask, targetMs, hardMs); break; }
                    catch (WebException ex)
                    {
                        if (attempt >= cfg.RetryCount || !Throttled(ex)) throw;
                        Diag.Write("download throttled at " + ask + " bytes, retrying smaller: " + ex.Message);
                        if (cfg.RetryDelayMs > 0) Thread.Sleep(cfg.RetryDelayMs);
                        ask = Math.Max(100000, ask / 2);
                    }
                }
            }
        }

        static void DownOnce(Meter m, Settings cfg, long ask, long targetMs, long hardMs)
        {
            var req = NewRequest(Config.CF_HOST + "/__down?bytes=" + ask.ToString(CultureInfo.InvariantCulture));
            using (var resp = req.GetResponse())
            using (var st = resp.GetResponseStream())
            {
                var buf = new byte[cfg.ReadBufferBytes];
                int n;
                while ((n = st.Read(buf, 0, buf.Length)) > 0)
                {
                    m.MarkFirstByte();
                    m.Add(n);
                    if (m.Done(targetMs, hardMs)) break;
                }
            }
        }

        static void Up(Meter m, Settings cfg, long bytes, long targetMs, long hardMs)
        {
            var buf = new byte[cfg.WriteChunkBytes];
            for (int i = 0; i < buf.Length; i++) buf[i] = (byte)(i * 31 + 7);

            long remaining = bytes;
            while (remaining > 0 && !m.Done(targetMs, hardMs))
            {
                long ask = Math.Min(remaining, cfg.RequestBytesMax);
                remaining -= ask;
                bool stop = false;
                for (int attempt = 0; ; attempt++)
                {
                    try { stop = UpOnce(m, cfg, buf, ask, targetMs, hardMs); break; }
                    catch (WebException ex)
                    {
                        if (attempt >= cfg.RetryCount || !Throttled(ex)) throw;
                        Diag.Write("upload throttled at " + ask + " bytes, retrying smaller: " + ex.Message);
                        if (cfg.RetryDelayMs > 0) Thread.Sleep(cfg.RetryDelayMs);
                        ask = Math.Max(100000, ask / 2);
                    }
                }
                if (stop) break;
            }
        }

        // Returns true when the stop condition tripped mid-request, so the caller stops asking.
        static bool UpOnce(Meter m, Settings cfg, byte[] buf, long ask, long targetMs, long hardMs)
        {
            var req = NewRequest(Config.CF_HOST + "/__up");
            req.Method = "POST";
            req.ContentType = "application/octet-stream";
            req.ContentLength = ask;
            req.AllowWriteStreamBuffering = false;   // put bytes on the wire as we write them

            bool aborted = false;
            try
            {
                using (var st = req.GetRequestStream())
                {
                    long left = ask;
                    while (left > 0)
                    {
                        int n = (int)Math.Min(left, buf.Length);
                        m.MarkFirstByte();          // the clock starts when we begin pushing, not after
                        st.Write(buf, 0, n);
                        m.Add(n);
                        left -= n;
                        if (m.Done(targetMs, hardMs)) { aborted = true; break; }
                    }
                }
                if (aborted) req.Abort();
                else using (req.GetResponse()) { }
            }
            catch (WebException) { if (!aborted) throw; }   // abort surfaces as WebException
            return aborted;
        }

        // ---- optional engine: Ookla speedtest CLI ----
        static void RunOokla(Settings s, Result r)
        {
            if (string.IsNullOrEmpty(s.OoklaPath) || !File.Exists(s.OoklaPath))
            {
                r.Error = "ookla_path not set - falling back to cloudflare";
                Diag.Write(r.Error);
                r.Engine = "cloudflare";
                r.Error = "";
                RunCloudflare(s, r);
                return;
            }

            var psi = new ProcessStartInfo(s.OoklaPath, "--accept-license --accept-gdpr --format=json");
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.CreateNoWindow = true;
            string json, err;
            using (var p = Process.Start(psi))
            {
                json = p.StandardOutput.ReadToEnd();
                err = p.StandardError.ReadToEnd();
                if (!p.WaitForExit(Math.Max(120000, s.MaxTestSeconds * 4000))) { try { p.Kill(); } catch { } }
            }

            double bwDown = Num(json, @"""download""\s*:\s*\{[^}]*?""bandwidth""\s*:\s*([\d.]+)");
            double bwUp = Num(json, @"""upload""\s*:\s*\{[^}]*?""bandwidth""\s*:\s*([\d.]+)");
            r.DownMbps = bwDown * 8.0 / 1e6;               // CLI reports bytes/s
            r.UpMbps = bwUp * 8.0 / 1e6;
            r.LatencyMs = Num(json, @"""ping""\s*:\s*\{[^}]*?""latency""\s*:\s*([\d.]+)");
            r.JitterMs = Num(json, @"""ping""\s*:\s*\{[^}]*?""jitter""\s*:\s*([\d.]+)");
            r.DownBytes = (long)Num(json, @"""download""\s*:\s*\{[^}]*?""bytes""\s*:\s*([\d.]+)");
            r.UpBytes = (long)Num(json, @"""upload""\s*:\s*\{[^}]*?""bytes""\s*:\s*([\d.]+)");
            r.Server = Str(json, @"""server""\s*:\s*\{[^}]*?""name""\s*:\s*""([^""]*)""");

            if (r.DownMbps <= 0 && r.UpMbps <= 0)
                r.Error = "ookla output unparsed" + (err.Trim().Length > 0 ? ": " + err.Trim() : "");
        }

        static double Num(string s, string pattern)
        {
            Match m = Regex.Match(s, pattern, RegexOptions.Singleline);
            double d;
            if (m.Success && double.TryParse(m.Groups[1].Value, NumberStyles.Float, CultureInfo.InvariantCulture, out d)) return d;
            return 0;
        }

        static string Str(string s, string pattern)
        {
            Match m = Regex.Match(s, pattern, RegexOptions.Singleline);
            return m.Success ? m.Groups[1].Value : "";
        }
    }

    // ---- results.csv (append-only, plain text) ----
    static class Log
    {
        const string HEADER = "timestamp_utc,down_mbps,up_mbps,latency_ms,jitter_ms,down_bytes,up_bytes,network,metered,engine,server,error,down_seconds,up_seconds";
        static readonly object _gate = new object();

        public static void Append(Result r)
        {
            try
            {
                lock (_gate)
                {
                    bool fresh = !File.Exists(Paths.Results) || new FileInfo(Paths.Results).Length == 0;
                    var sb = new StringBuilder();
                    if (fresh) sb.AppendLine(HEADER);
                    sb.Append(r.TimestampUtc.ToString("o", CultureInfo.InvariantCulture)).Append(',');
                    sb.Append(Num(r.DownMbps, r.Ok)).Append(',');
                    sb.Append(Num(r.UpMbps, r.Ok)).Append(',');
                    sb.Append(Num(r.LatencyMs, r.Ok)).Append(',');
                    sb.Append(Num(r.JitterMs, r.Ok)).Append(',');
                    sb.Append(r.DownBytes.ToString(CultureInfo.InvariantCulture)).Append(',');
                    sb.Append(r.UpBytes.ToString(CultureInfo.InvariantCulture)).Append(',');
                    sb.Append(Csv(r.Network)).Append(',');
                    sb.Append(r.Metered ? "yes" : "no").Append(',');
                    sb.Append(Csv(r.Engine)).Append(',');
                    sb.Append(Csv(r.Server)).Append(',');
                    sb.Append(Csv(r.Error)).Append(',');
                    sb.Append(Num(r.DownSeconds, r.Ok)).Append(',');
                    sb.Append(Num(r.UpSeconds, r.Ok));
                    sb.AppendLine();
                    File.AppendAllText(Paths.Results, sb.ToString());
                }
            }
            catch (Exception ex) { Diag.Write("csv append failed: " + ex.Message); }
        }

        static string Num(double v, bool ok)
        {
            if (!ok && v <= 0) return "";
            return v.ToString("0.###", CultureInfo.InvariantCulture);
        }

        static string Csv(string v)
        {
            if (v == null) return "";
            if (v.IndexOfAny(new[] { ',', '"', '\r', '\n' }) < 0) return v;
            return "\"" + v.Replace("\"", "\"\"") + "\"";
        }

        // Rows as raw field arrays, header skipped. Used by the report.
        public static List<string[]> Read()
        {
            var rows = new List<string[]>();
            try
            {
                if (!File.Exists(Paths.Results)) return rows;
                foreach (string line in File.ReadAllLines(Paths.Results))
                {
                    if (line.Length == 0) continue;
                    if (line.StartsWith("timestamp_utc", StringComparison.OrdinalIgnoreCase)) continue;
                    rows.Add(ParseLine(line));
                }
            }
            catch (Exception ex) { Diag.Write("csv read failed: " + ex.Message); }
            return rows;
        }

        static string[] ParseLine(string line)
        {
            var fields = new List<string>();
            var cur = new StringBuilder();
            bool quoted = false;
            for (int i = 0; i < line.Length; i++)
            {
                char c = line[i];
                if (quoted)
                {
                    if (c == '"')
                    {
                        if (i + 1 < line.Length && line[i + 1] == '"') { cur.Append('"'); i++; }
                        else quoted = false;
                    }
                    else cur.Append(c);
                }
                else if (c == '"') quoted = true;
                else if (c == ',') { fields.Add(cur.ToString()); cur.Length = 0; }
                else cur.Append(c);
            }
            fields.Add(cur.ToString());
            while (fields.Count < 14) fields.Add("");   // rows written before down_seconds/up_seconds existed
            return fields.ToArray();
        }

        // Most recent successful result, for the tray status line.
        public static Result Last()
        {
            var rows = Read();
            for (int i = rows.Count - 1; i >= 0; i--)
            {
                string[] f = rows[i];
                if (f[11].Length > 0) continue;
                var r = new Result();
                r.TimestampUtc = D(f[0]);
                r.DownMbps = N(f[1]); r.UpMbps = N(f[2]); r.LatencyMs = N(f[3]); r.JitterMs = N(f[4]);
                r.DownBytes = (long)N(f[5]); r.UpBytes = (long)N(f[6]);
                r.Network = f[7]; r.Metered = f[8] == "yes"; r.Engine = f[9]; r.Server = f[10];
                r.DownSeconds = N(f[12]); r.UpSeconds = N(f[13]);
                return r;
            }
            return null;
        }

        static double N(string v) { double d; return double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d) ? d : 0; }
        static DateTime D(string v)
        {
            DateTime d;
            if (DateTime.TryParse(v, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out d)) return d;
            return DateTime.MinValue;
        }
    }

    // ---- report.html, generated from portal.html + the CSV ----
    static class Portal
    {
        public static string Build(Settings s)
        {
            string tpl = Template();
            var sb = new StringBuilder();
            sb.Append("[");
            var rows = Log.Read();
            for (int i = 0; i < rows.Count; i++)
            {
                string[] f = rows[i];
                if (i > 0) sb.Append(",\n");
                sb.Append("[").Append(Epoch(f[0])).Append(',')
                  .Append(JsNum(f[1])).Append(',')
                  .Append(JsNum(f[2])).Append(',')
                  .Append(JsNum(f[3])).Append(',')
                  .Append(JsNum(f[4])).Append(',')
                  .Append(JsNum(f[5])).Append(',')
                  .Append(JsNum(f[6])).Append(',')
                  .Append(JsStr(f[7])).Append(',')
                  .Append(f[8] == "yes" ? "1" : "0").Append(',')
                  .Append(JsStr(f[9])).Append(',')
                  .Append(JsStr(f[10])).Append(',')
                  .Append(JsStr(f[11])).Append(',')
                  .Append(JsNum(f[12])).Append(',')
                  .Append(JsNum(f[13])).Append("]");
            }
            sb.Append("]");

            string meta = "{interval:" + s.IntervalMinutes.ToString(CultureInfo.InvariantCulture)
                + ",paused:" + (s.Paused ? "true" : "false")
                + ",engine:" + JsStr(s.Engine)
                + ",generated:" + JsStr(DateTime.Now.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture)) + "}";

            string html = tpl.Replace("/*__DATA__*/[]", sb.ToString()).Replace("/*__META__*/{}", meta);
            File.WriteAllText(Paths.Report, html, new UTF8Encoding(false));
            return Paths.Report;
        }

        static string Template()
        {
            var asm = Assembly.GetExecutingAssembly();
            using (Stream st = asm.GetManifestResourceStream("portal.html"))
            {
                if (st != null)
                    using (var sr = new StreamReader(st, Encoding.UTF8)) return sr.ReadToEnd();
            }
            // Dev convenience: fall back to the file next to the exe or the source tree.
            string side = Path.Combine(Path.GetDirectoryName(Application.ExecutablePath), "portal.html");
            if (File.Exists(side)) return File.ReadAllText(side);
            throw new FileNotFoundException("portal.html template not found (embedded resource missing).");
        }

        static string Epoch(string iso)
        {
            DateTime d;
            if (!DateTime.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out d)) return "0";
            var t = d - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);
            return ((long)t.TotalMilliseconds).ToString(CultureInfo.InvariantCulture);
        }

        static string JsNum(string v)
        {
            double d;
            if (v.Length == 0 || !double.TryParse(v, NumberStyles.Float, CultureInfo.InvariantCulture, out d)) return "null";
            return d.ToString("0.######", CultureInfo.InvariantCulture);
        }

        static string JsStr(string v)
        {
            if (v == null) v = "";
            var sb = new StringBuilder("\"");
            foreach (char c in v)
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c == '\n') sb.Append("\\n");
                else if (c == '\r') sb.Append("\\r");
                else if (c == '<') sb.Append("\\u003c");
                else if (c < ' ') sb.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                else sb.Append(c);
            }
            return sb.Append('"').ToString();
        }
    }

    static class Fmt
    {
        public static string Bytes(double b)
        {
            string[] u = { "B", "KB", "MB", "GB", "TB" };
            int i = 0;
            while (b >= 1024 && i < u.Length - 1) { b /= 1024; i++; }
            return b.ToString(b < 10 && i > 0 ? "0.0" : "0", CultureInfo.InvariantCulture) + " " + u[i];
        }

        public static string Mbps(double v)
        {
            return v.ToString(v < 10 ? "0.0" : "0", CultureInfo.InvariantCulture);
        }
    }

    // Small modal used by "Frequency > Custom...".
    static class Prompt
    {
        public static string Show(string title, string label, string value)
        {
            using (var f = new Form())
            {
                f.Text = title;
                f.FormBorderStyle = FormBorderStyle.FixedDialog;
                f.StartPosition = FormStartPosition.CenterScreen;
                f.MinimizeBox = false; f.MaximizeBox = false; f.ShowInTaskbar = false; f.TopMost = true;
                f.ClientSize = new Size(330, 116);
                var lb = new Label { Text = label, Left = 12, Top = 12, Width = 306, Height = 32 };
                var tb = new TextBox { Text = value, Left = 12, Top = 48, Width = 306 };
                var ok = new Button { Text = "OK", DialogResult = DialogResult.OK, Left = 162, Top = 80, Width = 75 };
                var no = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Left = 243, Top = 80, Width = 75 };
                f.Controls.AddRange(new Control[] { lb, tb, ok, no });
                f.AcceptButton = ok; f.CancelButton = no;
                tb.SelectAll();
                return f.ShowDialog() == DialogResult.OK ? tb.Text.Trim() : null;
            }
        }
    }

    class TrayContext : ApplicationContext
    {
        const string RUN_KEY = @"Software\Microsoft\Windows\CurrentVersion\Run";

        static readonly int[] PRESET_MINUTES = { 5, 15, 30, 60, 120, 360, 720, 1440 };

        readonly NotifyIcon _tray = new NotifyIcon();
        readonly Form _sync = new Form();          // hidden, marshals events to the UI thread
        readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();

        ToolStripLabel _statusItem, _lastItem;
        ToolStripMenuItem _runItem, _pauseItem, _freqMenu, _netMenu, _meteredItem, _engineMenu, _startupItem;

        Settings _cfg;
        Icon _icon;
        Net.Info _net;
        Result _last;
        bool _running;
        DateTime _notBeforeUtc;                   // startup delay gate

        public TrayContext()
        {
            var h = _sync.Handle;                 // force handle creation so BeginInvoke works
            _cfg = Settings.Load();
            _net = Net.Current();
            _last = Log.Last();
            _notBeforeUtc = DateTime.UtcNow.AddSeconds(_cfg.StartupDelaySeconds);

            BuildMenu();
            _tray.Visible = true;
            _tray.MouseClick += delegate(object s, MouseEventArgs e) { if (e.Button == MouseButtons.Left) Refresh(); };
            _tray.MouseDoubleClick += delegate(object s, MouseEventArgs e) { if (e.Button == MouseButtons.Left) OpenReport(); };

            _timer.Interval = Config.TICK_MS;
            _timer.Tick += delegate { Tick(); };
            _timer.Start();

            SystemEvents.PowerModeChanged += OnPowerModeChanged;
            NetworkChange.NetworkAddressChanged += OnNetworkChanged;

            UpdateUi();
        }

        // ---- menu ----
        void BuildMenu()
        {
            var menu = new ContextMenuStrip();
            _statusItem = new ToolStripLabel { Enabled = false };
            _lastItem = new ToolStripLabel { Enabled = false };
            _runItem = new ToolStripMenuItem("Run test now", null, delegate { StartTest(true); });
            _pauseItem = new ToolStripMenuItem("Pause", null, delegate { TogglePause(); });
            _freqMenu = new ToolStripMenuItem("Frequency");
            _netMenu = new ToolStripMenuItem("Networks");
            _meteredItem = new ToolStripMenuItem("Skip metered networks", null, delegate { ToggleMetered(); });
            _engineMenu = new ToolStripMenuItem("Engine");
            _startupItem = new ToolStripMenuItem("Start with Windows", null, delegate { ToggleStartup(); });

            menu.Items.Add(_statusItem);
            menu.Items.Add(_lastItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(_runItem);
            menu.Items.Add(new ToolStripMenuItem("Open report", null, delegate { OpenReport(); }));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(_pauseItem);
            menu.Items.Add(_freqMenu);
            menu.Items.Add(_netMenu);
            menu.Items.Add(_meteredItem);
            menu.Items.Add(_engineMenu);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(_startupItem);
            menu.Items.Add(new ToolStripMenuItem("Open data folder", null, delegate { Open(Paths.Dir); }));
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(new ToolStripMenuItem("Exit", null, delegate { ExitApp(); }));

            menu.Opening += delegate { _net = Net.Current(); RebuildSubmenus(); UpdateUi(); };
            _tray.ContextMenuStrip = menu;
        }

        void RebuildSubmenus()
        {
            _freqMenu.DropDownItems.Clear();
            foreach (int m in PRESET_MINUTES)
            {
                int mm = m;   // capture per-iteration
                _freqMenu.DropDownItems.Add(new ToolStripMenuItem(FreqLabel(m), null, delegate { SetInterval(mm); }) { Checked = _cfg.IntervalMinutes == m });
            }
            _freqMenu.DropDownItems.Add(new ToolStripSeparator());
            bool custom = Array.IndexOf(PRESET_MINUTES, _cfg.IntervalMinutes) < 0;
            _freqMenu.DropDownItems.Add(new ToolStripMenuItem("Custom...", null, delegate { CustomInterval(); }) { Checked = custom });

            _netMenu.DropDownItems.Clear();
            _netMenu.DropDownItems.Add(new ToolStripMenuItem("Current: " + _net.Name + (_net.Metered ? " (metered)" : "")) { Enabled = false });
            _netMenu.DropDownItems.Add(new ToolStripSeparator());
            _netMenu.DropDownItems.Add(new ToolStripMenuItem("Run on any network", null, delegate { ClearNetworks(); }) { Checked = _cfg.OnlyNetworks.Count == 0 });
            bool listed = _cfg.OnlyNetworks.Contains(_net.Name);
            _netMenu.DropDownItems.Add(new ToolStripMenuItem("Only run on \"" + _net.Name + "\"", null, delegate { AddNetwork(_net.Name); }) { Checked = listed });
            if (_cfg.OnlyNetworks.Count > 0)
            {
                _netMenu.DropDownItems.Add(new ToolStripSeparator());
                _netMenu.DropDownItems.Add(new ToolStripMenuItem("Allowed (click to remove)") { Enabled = false });
                foreach (string n in _cfg.OnlyNetworks)
                {
                    string nn = n;
                    _netMenu.DropDownItems.Add(new ToolStripMenuItem(n, null, delegate { RemoveNetwork(nn); }) { Checked = true });
                }
            }

            _engineMenu.DropDownItems.Clear();
            _engineMenu.DropDownItems.Add(new ToolStripMenuItem("Cloudflare (built-in)", null, delegate { SetEngine("cloudflare"); }) { Checked = _cfg.Engine == "cloudflare" });
            bool ookla = _cfg.OoklaPath.Length > 0 && File.Exists(_cfg.OoklaPath);
            _engineMenu.DropDownItems.Add(new ToolStripMenuItem(ookla ? "Ookla CLI" : "Ookla CLI (set ookla_path)", null, delegate { SetEngine("ookla"); })
            {
                Checked = _cfg.Engine == "ookla",
                Enabled = ookla
            });
        }

        static string FreqLabel(int minutes)
        {
            if (minutes < 60) return minutes.ToString(CultureInfo.InvariantCulture) + " min";
            if (minutes % 1440 == 0) return (minutes / 1440).ToString(CultureInfo.InvariantCulture) + (minutes == 1440 ? " day" : " days");
            if (minutes % 60 == 0) return (minutes / 60).ToString(CultureInfo.InvariantCulture) + (minutes == 60 ? " hour" : " hours");
            return minutes.ToString(CultureInfo.InvariantCulture) + " min";
        }

        // ---- settings actions ----
        void SetInterval(int minutes) { _cfg.IntervalMinutes = minutes; _cfg.Save(); UpdateUi(); }

        void CustomInterval()
        {
            string v = Prompt.Show(Config.APP_NAME, "Run a test every N minutes:", _cfg.IntervalMinutes.ToString(CultureInfo.InvariantCulture));
            int n;
            if (v != null && int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) && n >= 1) SetInterval(n);
        }

        void TogglePause()
        {
            _cfg.Paused = !_cfg.Paused;
            _cfg.Save();
            UpdateUi();
        }

        void ToggleMetered() { _cfg.SkipMetered = !_cfg.SkipMetered; _cfg.Save(); UpdateUi(); }

        void SetEngine(string e) { _cfg.Engine = e; _cfg.Save(); UpdateUi(); }

        void ClearNetworks() { _cfg.OnlyNetworks.Clear(); _cfg.Save(); UpdateUi(); }

        void AddNetwork(string name)
        {
            if (_cfg.OnlyNetworks.Contains(name)) _cfg.OnlyNetworks.Remove(name);
            else _cfg.OnlyNetworks.Add(name);
            _cfg.Save();
            UpdateUi();
        }

        void RemoveNetwork(string name) { _cfg.OnlyNetworks.Remove(name); _cfg.Save(); UpdateUi(); }

        // ---- autostart (HKCU Run) ----
        bool IsAutostart()
        {
            using (var k = Registry.CurrentUser.OpenSubKey(RUN_KEY))
                return k != null && k.GetValue(Config.APP_NAME) != null;
        }

        void ToggleStartup()
        {
            try
            {
                using (var k = Registry.CurrentUser.OpenSubKey(RUN_KEY, true))
                {
                    if (k == null) return;
                    if (IsAutostart()) k.DeleteValue(Config.APP_NAME, false);
                    else k.SetValue(Config.APP_NAME, "\"" + Application.ExecutablePath + "\"");
                }
            }
            catch (Exception ex) { Diag.Write("autostart toggle failed: " + ex.Message); }
            UpdateUi();
        }

        // ---- scheduling ----
        void Tick()
        {
            if (_running || _cfg.Paused) return;
            if (DateTime.UtcNow < _notBeforeUtc) return;
            DateTime due = _cfg.LastRun == DateTime.MinValue ? DateTime.MinValue : _cfg.LastRun.AddMinutes(_cfg.IntervalMinutes);
            if (DateTime.UtcNow < due) return;
            StartTest(false);
        }

        // Guards, in order. A blocked network is logged once per interval, not per tick.
        string Blocked()
        {
            if (_cfg.Paused) return "skipped: paused";
            if (_cfg.SkipMetered && _net.Metered) return "skipped: metered";
            if (!_cfg.NetworkAllowed(_net.Name)) return "skipped: network not allowed";
            return null;
        }

        void StartTest(bool manual)
        {
            if (_running) return;
            _net = Net.Current();

            string block = Blocked();
            if (block != null)
            {
                // A scheduled attempt is logged as a gap with its reason and burns the interval,
                // so a blocked network cannot cause a retry storm. A manual click just says why.
                if (manual)
                {
                    _tray.ShowBalloonTip(4000, Config.APP_NAME, block.Substring("skipped: ".Length) + " - no test run.", ToolTipIcon.Info);
                    return;
                }
                var skip = new Result { Network = _net.Name, Metered = _net.Metered, Engine = _cfg.Engine, Error = block };
                Log.Append(skip);
                _cfg.LastRun = DateTime.UtcNow;
                _cfg.Save();
                UpdateUi();
                return;
            }

            if (!_net.HasInternet)
            {
                // Transient: do not burn the interval, just wait for the next tick.
                if (manual) _tray.ShowBalloonTip(4000, Config.APP_NAME, "No internet connection.", ToolTipIcon.Warning);
                return;
            }

            _running = true;
            UpdateUi();
            var cfg = _cfg;
            var net = _net;
            var th = new Thread(delegate()
            {
                Result r = SpeedTest.Run(cfg, net);
                Post(delegate { Finish(r, manual); });
            });
            th.IsBackground = true;
            th.Start();
        }

        void Finish(Result r, bool manual)
        {
            _running = false;
            Log.Append(r);
            _cfg.LastRun = r.TimestampUtc;
            _cfg.Save();
            if (r.Ok) _last = r;
            if (!r.Ok && manual) _tray.ShowBalloonTip(5000, Config.APP_NAME, "Test failed: " + r.Error, ToolTipIcon.Warning);
            Diag.Write("result " + Fmt.Mbps(r.DownMbps) + "/" + Fmt.Mbps(r.UpMbps) + " Mbps over "
                + r.DownSeconds.ToString("0.0", CultureInfo.InvariantCulture) + "/" + r.UpSeconds.ToString("0.0", CultureInfo.InvariantCulture) + " s, "
                + r.LatencyMs.ToString("0.0", CultureInfo.InvariantCulture) + " ms, " + r.DownBytes + "+" + r.UpBytes + " bytes, net=" + r.Network
                + (r.Ok ? "" : ", error=" + r.Error));
            UpdateUi();
        }

        void Refresh() { _net = Net.Current(); UpdateUi(); }

        // ---- report ----
        void OpenReport()
        {
            try { Open(Portal.Build(_cfg)); }
            catch (Exception ex)
            {
                Diag.Write("report failed: " + ex.Message);
                _tray.ShowBalloonTip(5000, Config.APP_NAME, "Could not build report: " + ex.Message, ToolTipIcon.Error);
            }
        }

        static void Open(string path)
        {
            try { Process.Start(new ProcessStartInfo(path) { UseShellExecute = true }); }
            catch (Exception ex) { Diag.Write("open failed: " + path + " - " + ex.Message); }
        }

        // ---- UI ----
        void UpdateUi()
        {
            string state = _running ? "testing..." : (_cfg.Paused ? "PAUSED" : "every " + FreqLabel(_cfg.IntervalMinutes));
            string netTag = _net.Name + (_net.Metered ? " · metered" : "") + (_net.OverLimit ? " · over limit" : (_net.ApproachingLimit ? " · near limit" : ""));

            if (_last != null)
                _statusItem.Text = Fmt.Mbps(_last.DownMbps) + " Mbps down · " + Fmt.Mbps(_last.UpMbps) + " up · " + _last.LatencyMs.ToString("0", CultureInfo.InvariantCulture) + " ms";
            else
                _statusItem.Text = "No result yet";

            if (_last != null)
                _lastItem.Text = _last.TimestampUtc.ToLocalTime().ToString("MMM d HH:mm", CultureInfo.InvariantCulture)
                    + " · " + Fmt.Bytes(_last.DownBytes) + " down / " + Fmt.Bytes(_last.UpBytes) + " up · " + state;
            else
                _lastItem.Text = state;

            _runItem.Enabled = !_running;
            _runItem.Text = _running ? "Running test..." : "Run test now";
            _pauseItem.Checked = _cfg.Paused;
            _pauseItem.Text = _cfg.Paused ? "Paused (click to resume)" : "Pause";
            _freqMenu.Text = "Frequency (" + FreqLabel(_cfg.IntervalMinutes) + ")";
            _netMenu.Text = "Networks (" + (_cfg.OnlyNetworks.Count == 0 ? "any" : _cfg.OnlyNetworks.Count.ToString(CultureInfo.InvariantCulture) + " allowed") + ")";
            _meteredItem.Checked = _cfg.SkipMetered;
            _engineMenu.Text = "Engine (" + _cfg.Engine + ")";
            _startupItem.Checked = IsAutostart();

            string tip = Config.APP_NAME + " - " + (_last != null ? Fmt.Mbps(_last.DownMbps) + "/" + Fmt.Mbps(_last.UpMbps) + " Mbps · " : "") + netTag;
            if (tip.Length > 63) tip = tip.Substring(0, 63);
            _tray.Text = tip;

            SetIcon();
        }

        void SetIcon()
        {
            Color bg;
            if (_running) bg = Color.FromArgb(42, 120, 214);                      // blue while testing
            else if (_cfg.Paused) bg = Color.FromArgb(219, 154, 4);               // amber paused
            else if (_last == null) bg = Color.FromArgb(137, 135, 129);           // grey never run
            else bg = Color.FromArgb(46, 160, 67);                                // green have result

            string txt = _last != null ? Fmt.Mbps(_last.DownMbps) : "--";
            if (_running) txt = "...";
            var newIcon = MakeIcon(txt, bg);
            _tray.Icon = newIcon;
            if (_icon != null) _icon.Dispose();
            _icon = newIcon;
        }

        static Icon MakeIcon(string txt, Color bg)
        {
            using (var bmp = new Bitmap(32, 32))
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
                g.Clear(Color.Transparent);
                using (var b = new SolidBrush(bg)) g.FillEllipse(b, 0, 0, 31, 31);
                float size = txt.Length >= 4 ? 9f : (txt.Length >= 3 ? 11f : 14f);
                using (var f = new Font("Segoe UI", size, FontStyle.Bold, GraphicsUnit.Pixel))
                using (var tb = new SolidBrush(Color.White))
                {
                    var fmt = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
                    g.DrawString(txt, f, tb, new RectangleF(0, 0, 32, 32), fmt);
                }
                IntPtr hIcon = bmp.GetHicon();
                Icon ico = (Icon)Icon.FromHandle(hIcon).Clone();
                Native.DestroyIcon(hIcon);
                return ico;
            }
        }

        // ---- system events (fire off the UI thread) ----
        void OnPowerModeChanged(object sender, PowerModeChangedEventArgs e)
        {
            if (e.Mode == PowerModes.Resume) Post(delegate { Refresh(); Tick(); });
        }

        void OnNetworkChanged(object sender, EventArgs e)
        {
            Post(delegate { Refresh(); Tick(); });
        }

        void Post(Action a)
        {
            try { if (_sync.IsHandleCreated) _sync.BeginInvoke(a); else a(); }
            catch { /* shutting down */ }
        }

        void ExitApp()
        {
            SystemEvents.PowerModeChanged -= OnPowerModeChanged;
            NetworkChange.NetworkAddressChanged -= OnNetworkChanged;
            _timer.Stop();
            _timer.Dispose();
            _tray.Visible = false;
            _tray.Dispose();
            if (_icon != null) _icon.Dispose();
            _sync.Dispose();
            ExitThread();
        }
    }

    static class Program
    {
        [STAThread]
        static void Main()
        {
            bool created;
            using (var mtx = new Mutex(true, "Speedster_SingleInstance_9f31ad42", out created))
            {
                if (!created) return;   // already running
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new TrayContext());
            }
        }
    }
}
