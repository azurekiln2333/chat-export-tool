// Isolated x86 codec host. Only private copies are opened by the native codec.
// Function RVAs below were verified from this exact, hash-pinned QQ component.
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Web.Script.Serialization;

internal static class LegacyQq
{
    sealed class CodecFailure : Exception
    { public CodecFailure(string code) : base(code) { } }
    const string KernelHash = "46f2cd17c647ed0b2aad1c0c448135fbd4485eebb6d14392e9223257e1aee9c2";
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibraryEx(string path, IntPtr file, uint flags);
    [DllImport("kernel32.dll")] static extern uint SetErrorMode(uint mode);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int OpenDb(byte[] name, out IntPtr db);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int KeyDb(IntPtr db, byte[] key, int length);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int CloseDb(IntPtr db);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int InitHeaderDb(IntPtr db, int size, IntPtr result);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int RowCallback(IntPtr context, int columns, IntPtr values, IntPtr names);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int ExecDb(IntPtr db, byte[] sql, RowCallback callback, IntPtr context, IntPtr error);
    static OpenDb Open;
    static KeyDb Key, Rekey;
    static CloseDb Close;
    static ExecDb Exec;
    static InitHeaderDb InitHeader;

    static void Emit(object value) { Console.WriteLine(Json.Serialize(value)); Console.Out.Flush(); }
    static byte[] Utf8(string text) { return Encoding.UTF8.GetBytes(text + "\0"); }
    static string Hash(string path)
    {
        using (var stream = File.OpenRead(path))
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }
    static T Bind<T>(IntPtr module, int rva) where T : class
    { return Marshal.GetDelegateForFunctionPointer(IntPtr.Add(module, rva), typeof(T)) as T; }

    static void Load(string directory)
    {
        if (IntPtr.Size != 4) throw new CodecFailure("wrong_architecture");
        var dll = Path.Combine(Path.GetFullPath(directory), "KernelUtil.dll");
        if (!File.Exists(dll)) throw new CodecFailure("missing_component");
        if (Hash(dll) != KernelHash) throw new CodecFailure("unsupported_component");
        // Search only the explicitly selected bundle and Windows system directory.
        var module = LoadLibraryEx(dll, IntPtr.Zero, 0x100 | 0x800);
        if (module == IntPtr.Zero)
            throw new CodecFailure("load_component_" + Marshal.GetLastWin32Error());
        Open = Bind<OpenDb>(module, 0x365cf);
        Key = Bind<KeyDb>(module, 0x86eb2);
        Rekey = Bind<KeyDb>(module, 0x87071);
        Close = Bind<CloseDb>(module, 0x3596e);
        // execDML's five-argument call target. 0x35ce1 is sqlite3_changes, NOT exec.
        Exec = Bind<ExecDb>(module, 0x35990);
        InitHeader = Bind<InitHeaderDb>(module, 0x86a1b);
    }

    static void Check(int rc, string stage)
    { if (rc != 0) throw new CodecFailure(stage + "_" + rc); }
    static void Sql(IntPtr db, string sql)
    { Check(Exec(db, Utf8(sql), null, IntPtr.Zero, IntPtr.Zero), "sql"); }
    static void Integrity(IntPtr db)
    {
        int rows = 0;
        bool valid = true;
        RowCallback callback = delegate(IntPtr context, int columns, IntPtr values, IntPtr names) {
            rows++;
            valid &= columns == 1 && Marshal.PtrToStringAnsi(Marshal.ReadIntPtr(values)) == "ok";
            return 0;
        };
        Check(Exec(db, Utf8("PRAGMA integrity_check"), callback, IntPtr.Zero, IntPtr.Zero), "verify");
        GC.KeepAlive(callback);
        if (!valid || rows != 1) throw new CodecFailure("integrity_failed");
    }
    static byte[] ParseKey(string value)
    {
        if (value == null || value.Length != 32) throw new CodecFailure("invalid_key");
        var result = new byte[16];
        try { for (int i = 0; i < 16; i++) result[i] = Convert.ToByte(value.Substring(i * 2, 2), 16); }
        catch (FormatException) { throw new CodecFailure("invalid_key"); }
        bool nonzero = false;
        foreach (byte b in result) nonzero |= b != 0;
        if (!nonzero) throw new CodecFailure("invalid_key");
        return result;
    }
    static void Copy(string source, string output)
    {
        using (var input = File.OpenRead(source))
        using (var target = new FileStream(output, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            var buffer = new byte[1024 * 1024];
            long done = 0;
            var last = DateTime.UtcNow;
            int count;
            while ((count = input.Read(buffer, 0, buffer.Length)) != 0)
            {
                target.Write(buffer, 0, count); done += count;
                if ((DateTime.UtcNow - last).TotalSeconds >= 1)
                {
                    Emit(new { type="progress", stage="copy", completed=done, total=input.Length });
                    last = DateTime.UtcNow;
                }
            }
            target.Flush(true);
            Emit(new { type="progress", stage="copy", completed=done, total=input.Length });
        }
    }
    static string Required(Dictionary<string, object> request, string field)
    {
        object value;
        if (!request.TryGetValue(field, out value) || !(value is string) || String.IsNullOrEmpty((string)value))
            throw new CodecFailure("invalid_request");
        return (string)value;
    }
    static void Decrypt(Dictionary<string, object> request)
    {
        var source = Path.GetFullPath(Required(request, "source"));
        var output = Path.GetFullPath(Required(request, "output"));
        var key = ParseKey(Required(request, "key"));
        if (String.Equals(source, output, StringComparison.OrdinalIgnoreCase) || File.Exists(output))
            throw new CodecFailure("output_exists");
        foreach (string suffix in new[] { "-wal", "-journal" })
            if (File.Exists(source + suffix) && new FileInfo(source + suffix).Length > 0)
                throw new CodecFailure("active_sidecar");
        Copy(source, output);
        Emit(new { type="progress", stage="verify_copy" });
        if (Hash(source) != Hash(output)) throw new CodecFailure("copy_mismatch");
        IntPtr db = IntPtr.Zero;
        try
        {
            Check(Open(Utf8(output), out db), "open");
            Check(Key(db, key, key.Length), "key");
            Emit(new { type="progress", stage="verify_key" });
            Sql(db, "SELECT count(*) FROM sqlite_master");
            Integrity(db);
            // Private output only. Avoid native rollback-file deletion as well.
            Sql(db, "PRAGMA journal_mode=OFF");
            Emit(new { type="progress", stage="decrypt" });
            Check(Rekey(db, new byte[16], 16), "rekey");
            Integrity(db);
        }
        finally
        {
            Array.Clear(key, 0, key.Length);
            if (db != IntPtr.Zero) Check(Close(db), "close");
        }
        Emit(new { type="result", ok=true });
    }
    static void Fixture(Dictionary<string, object> request)
    {
        // Bounded, public synthetic fixture; no user-controlled SQL or real key.
        var output = Path.GetFullPath(Required(request, "output"));
        if (File.Exists(output)) throw new CodecFailure("output_exists");
        using (var file = new FileStream(output, FileMode.CreateNew)) { }
        IntPtr db = IntPtr.Zero;
        try
        {
            Check(Open(Utf8(output), out db), "open");
            object wrapped;
            if (request.TryGetValue("wrapped", out wrapped) && (bool)wrapped)
                Check(InitHeader(db, 1024, IntPtr.Zero), "init_header");
            Check(Key(db, ParseKey("00112233445566778899aabbccddeeff"), 16), "key");
            Sql(db, "PRAGMA journal_mode=OFF; CREATE TABLE fixture(id INTEGER PRIMARY KEY, body TEXT, raw BLOB);" +
                    "INSERT INTO fixture VALUES(1,'synthetic Unicode 测试',X'0001FEFF');" +
                    "INSERT INTO fixture VALUES(2,'second row',NULL);");
            object messages;
            if (request.TryGetValue("messages", out messages) && (bool)messages)
                Sql(db, "CREATE TABLE mr_buddy_987654321(Time INTEGER,Rand INTEGER,SenderUin INTEGER,MsgContent BLOB,Info BLOB);" +
                    "INSERT INTO mr_buddy_987654321 VALUES(1700000000,7,123456789," +
                    "X'000000000000000000f1536540302010332211000c00860000000000011000010600604f3dd800de0104003c002600',X'00FF');");
            Integrity(db);
        }
        finally { if (db != IntPtr.Zero) Check(Close(db), "close"); }
        Emit(new { type="result", ok=true });
    }
    public static int Main(string[] args)
    {
        SetErrorMode(0x0001 | 0x0002 | 0x8000);
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            if (args.Length != 2) throw new CodecFailure("invalid_arguments");
            Load(args[1]);
            if (args[0] == "probe")
                Emit(new { type="result", ok=true, protocol=1, architecture="x86", component="9.7.23.29348" });
            else
            {
                string line = Console.ReadLine();
                if (line == null || line.Length > 32768) throw new CodecFailure("invalid_request");
                var request = Json.Deserialize<Dictionary<string, object>>(line);
                if (args[0] == "decrypt") Decrypt(request);
                else if (args[0] == "fixture") Fixture(request);
                else throw new CodecFailure("invalid_command");
            }
            return 0;
        }
        catch (CodecFailure error)
        {
            Emit(new { type="result", ok=false, error=error.Message });
            return 1;
        }
        catch (Exception)
        {
            // Do not print native error strings, paths, SQL or stdin/key material.
            Emit(new { type="result", ok=false, error="helper_failed" });
            return 1;
        }
    }
}
