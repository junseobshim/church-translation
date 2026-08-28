import Foundation
import Observation

/// Owns the main.py subprocess for the Phase 1 walking skeleton
/// (docs/native-macos-app-migration.md §Phase 1). Runs against the repo's
/// existing `venv/`, resolved relative to this source file's on-disk location —
/// that only works from a checked-out repo, not an installed .app bundle.
/// Swapping in `Bundle.main.resourcePath`-relative paths to a bundled
/// python-build-standalone runtime (§3 of the migration doc) replaces
/// `resolvePython()`/`resolveMainPy()` without touching anything else here.
@Observable
final class SessionController {
    enum State: Equatable {
        case idle
        case running(pid: Int32)
        case failed(String)
    }

    struct AudioDevice: Identifiable, Decodable, Equatable {
        let index: Int
        let name: String
        var id: Int { index }
    }

    struct CaptionLine: Identifiable {
        let id: Int
        let kind: String // "transcription" | "translation"
        let lang: String
        let text: String
    }

    struct TunnelOption: Identifiable, Equatable {
        let name: String
        let url: String?
        var id: String { name }
    }

    private struct CFTunnelListEntry: Decodable {
        let id: String
        let name: String
    }

    private struct RawCaptionLine: Decodable {
        let kind: String
        let lang: String
        let text: String
    }

    private struct LatestResponse: Decodable {
        let lines: [RawCaptionLine]
        let start: Int
        let total: Int
    }

    static let shared = SessionController()

    private(set) var state: State = .idle
    private(set) var log: String = ""
    private(set) var startedAt: Date?
    /// Mirrors control.html's live preview: last 12 lines from /api/latest,
    /// polled every 400ms while a session is running.
    private(set) var captionLines: [CaptionLine] = []

    /// Fired once after a running process has fully exited via stop(), then
    /// cleared. Used by AppDelegate to complete an in-flight app-quit only
    /// after the session has actually torn down (tunnel included).
    var onStopped: (() -> Void)?

    private var process: Process?
    private var stdoutPipe: Pipe?
    private var stderrPipe: Pipe?
    private var pollTask: Task<Void, Never>?
    private var pollLastCount = 0
    private var nextCaptionID = 0

    private func repoRoot() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // ChurchTranslation/
            .deletingLastPathComponent() // Sources/
            .deletingLastPathComponent() // app/
            .deletingLastPathComponent() // repo root
    }

    private func appDir() -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // ChurchTranslation/
            .deletingLastPathComponent() // Sources/
            .deletingLastPathComponent() // app/
    }

    /// Bundled python-build-standalone runtime (app/build/stage_python.sh).
    /// Priority: real .app bundle Resources (Bundle.main — what a double-clicked
    /// or `open`-launched Church Translation.app uses) > dev-mode Resources next
    /// to a bare `swift build` executable > repo venv, so `swift run`/direct-exec
    /// dev iteration still works without staging or bundling anything.
    private func resolvePython() -> URL? {
        if let resourcePath = Bundle.main.resourcePath {
            let bundled = URL(fileURLWithPath: resourcePath).appendingPathComponent("python/bin/python3")
            if FileManager.default.fileExists(atPath: bundled.path) {
                return bundled
            }
        }
        let devStaged = appDir().appendingPathComponent("Resources/python/bin/python3")
        if FileManager.default.fileExists(atPath: devStaged.path) {
            return devStaged
        }
        let venvPython = repoRoot().appendingPathComponent("venv/bin/python3")
        return FileManager.default.fileExists(atPath: venvPython.path) ? venvPython : nil
    }

    private func resolveMainPy() -> URL? {
        if let resourcePath = Bundle.main.resourcePath {
            let bundled = URL(fileURLWithPath: resourcePath).appendingPathComponent("app/main.py")
            if FileManager.default.fileExists(atPath: bundled.path) {
                return bundled
            }
        }
        let devStaged = appDir().appendingPathComponent("Resources/app/main.py")
        if FileManager.default.fileExists(atPath: devStaged.path) {
            return devStaged
        }
        let mainPy = repoRoot().appendingPathComponent("main.py")
        return FileManager.default.fileExists(atPath: mainPy.path) ? mainPy : nil
    }

    /// One-shot device enumeration via the same interpreter/PortAudio build
    /// main.py uses. Native CoreAudio/AVFoundation enumeration would hand back
    /// different device identifiers than PortAudio's indices — matching
    /// indices main.py will actually see requires going through the bundled
    /// interpreter, not native APIs (doc §2.2).
    func listAudioDevices() async -> [AudioDevice] {
        guard let python = resolvePython() else { return [] }
        let script = """
        import sounddevice, json
        print(json.dumps([
            {"index": i, "name": d["name"]}
            for i, d in enumerate(sounddevice.query_devices())
            if d["max_input_channels"] > 0
        ]))
        """
        let proc = Process()
        proc.executableURL = python
        proc.arguments = ["-c", script]
        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = Pipe() // discard PortAudio init noise

        return await withCheckedContinuation { continuation in
            proc.terminationHandler = { _ in
                let data = outPipe.fileHandleForReading.readDataToEndOfFile()
                let devices = (try? JSONDecoder().decode([AudioDevice].self, from: data)) ?? []
                continuation.resume(returning: devices)
            }
            do {
                try proc.run()
            } catch {
                continuation.resume(returning: [])
            }
        }
    }

    /// One-shot .docx-to-text extraction via the bundled python-docx (doc
    /// §2.3) — no Swift-side .docx/XML parsing. Returns nil on any failure.
    func extractDocxText(at url: URL) async -> String? {
        guard let python = resolvePython() else { return nil }
        let script = """
        import sys, docx
        doc = docx.Document(sys.argv[1])
        print("\\n".join(p.text for p in doc.paragraphs))
        """
        let proc = Process()
        proc.executableURL = python
        proc.arguments = ["-c", script, url.path]
        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = Pipe()

        return await withCheckedContinuation { continuation in
            proc.terminationHandler = { p in
                guard p.terminationStatus == 0 else {
                    continuation.resume(returning: nil)
                    return
                }
                let data = outPipe.fileHandleForReading.readDataToEndOfFile()
                let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
                continuation.resume(returning: (text?.isEmpty ?? true) ? nil : text)
            }
            do {
                try proc.run()
            } catch {
                continuation.resume(returning: nil)
            }
        }
    }

    /// Same fallback locations main.py's _resolve_cloudflared() checks after
    /// shutil.which — GUI-launched apps inherit a launchd PATH without
    /// Homebrew, so those hardcoded candidates matter more than PATH lookup.
    private func resolveCloudflared() -> URL? {
        for candidate in ["/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"] {
            if FileManager.default.fileExists(atPath: candidate) {
                return URL(fileURLWithPath: candidate)
            }
        }
        return nil
    }

    /// Order-preserving parse of tunnels.json's {"tunnels": {name: url, ...}}.
    /// JSONSerialization's Dictionary does not retain key order, but
    /// tunnels.json's own comment ("Order matters: the control panel selects
    /// the FIRST entry by default") depends on it — regex over the raw text
    /// instead, safe for this narrow, self-controlled file format.
    private func tunnelURLMapping() -> [(name: String, url: String)] {
        guard let mainPy = resolveMainPy() else { return [] }
        let tunnelsPath = mainPy.deletingLastPathComponent().appendingPathComponent("tunnels.json")
        guard let text = try? String(contentsOf: tunnelsPath, encoding: .utf8),
              let tunnelsKeyRange = text.range(of: "\"tunnels\"") else {
            return []
        }
        let after = String(text[tunnelsKeyRange.upperBound...]) as NSString
        guard let regex = try? NSRegularExpression(pattern: #""([\w-]+)"\s*:\s*"([^"]*)""#) else {
            return []
        }
        let matches = regex.matches(in: after as String, range: NSRange(location: 0, length: after.length))
        return matches.compactMap { m in
            guard m.numberOfRanges == 3 else { return nil }
            return (after.substring(with: m.range(at: 1)), after.substring(with: m.range(at: 2)))
        }
    }

    /// Tunnel UUIDs this device holds credentials for — a tunnel can only be
    /// *run* here if its <uuid>.json credentials file exists in ~/.cloudflared.
    private func localTunnelCredentialIDs() -> Set<String> {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cloudflared")
        guard let files = try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) else {
            return []
        }
        var ids = Set<String>()
        for file in files where file.pathExtension == "json" {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = obj["TunnelID"] as? String else { continue }
            ids.insert(id)
        }
        return ids
    }

    private func runCloudflaredTunnelList(_ cloudflared: URL) async throws -> [CFTunnelListEntry] {
        let proc = Process()
        proc.executableURL = cloudflared
        proc.arguments = ["tunnel", "list", "--output", "json"]
        let outPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = Pipe()

        return try await withCheckedThrowingContinuation { continuation in
            proc.terminationHandler = { p in
                guard p.terminationStatus == 0 else {
                    continuation.resume(throwing: CocoaError(.fileReadUnknown))
                    return
                }
                let data = outPipe.fileHandleForReading.readDataToEndOfFile()
                do {
                    continuation.resume(returning: try JSONDecoder().decode([CFTunnelListEntry].self, from: data))
                } catch {
                    continuation.resume(throwing: error)
                }
            }
            do {
                try proc.run()
                // 6s timeout, matching control_server.py's get_tunnels().
                DispatchQueue.global().asyncAfter(deadline: .now() + 6) {
                    if proc.isRunning { proc.terminate() }
                }
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }

    /// Ports control_server.py's get_tunnels(): only offer tunnels this
    /// machine actually holds credentials for, intersected with a live
    /// `cloudflared tunnel list` against the Cloudflare API. Falls back to
    /// the static tunnels.json mapping when cloudflared/network/credentials
    /// aren't available, so the picker still offers *something*.
    func listTunnels() async -> [TunnelOption] {
        let mapping = tunnelURLMapping()
        let order = mapping.map(\.name)
        let urlByName = Dictionary(uniqueKeysWithValues: mapping.map { ($0.name, $0.url) })

        if let cloudflared = resolveCloudflared(),
           let entries = try? await runCloudflaredTunnelList(cloudflared) {
            let localIDs = localTunnelCredentialIDs()
            var found = entries
                .filter { localIDs.contains($0.id) }
                .map { TunnelOption(name: $0.name, url: urlByName[$0.name]) }
            found.sort { a, b in
                let ai = order.firstIndex(of: a.name) ?? order.count
                let bi = order.firstIndex(of: b.name) ?? order.count
                return ai < bi
            }
            return found
        }

        return mapping.map { TunnelOption(name: $0.name, url: $0.url) }
    }

    func start(source: String, target: String, device: String, outlinePath: String? = nil, tunnel: String? = nil) {
        guard case .idle = state else { return }
        guard let python = resolvePython(), let mainPy = resolveMainPy() else {
            state = .failed("Could not find venv/bin/python3 or main.py next to the app checkout.")
            return
        }

        let proc = Process()
        proc.executableURL = python
        var args = [mainPy.path, "--source", source, "--target", target]
        if let tunnel, !tunnel.isEmpty {
            args += ["--tunnel", tunnel]
        } else {
            args += ["--no-tunnel"]
        }
        if !device.trimmingCharacters(in: .whitespaces).isEmpty {
            args += ["--device", device]
        }
        if let outlinePath, !outlinePath.isEmpty {
            args += ["--outline", outlinePath]
        }
        proc.arguments = args
        proc.currentDirectoryURL = mainPy.deletingLastPathComponent()

        // Keychain-sourced keys, exported as env vars (doc §2.3/§2.4) — only
        // set if present, so a Keychain that hasn't been configured yet still
        // falls back to main.py's own .env loading. Note: main.py calls
        // load_dotenv(override=True), so a repo-checkout .env found upward
        // from cwd wins over these during dev-mode testing; irrelevant for a
        // real installed .app with no ancestor .env to find.
        var env = ProcessInfo.processInfo.environment
        if let key = KeychainStore.get("SONIOX_API_KEY"), !key.isEmpty {
            env["SONIOX_API_KEY"] = key
        }
        if let key = KeychainStore.get("ANTHROPIC_API_KEY"), !key.isEmpty {
            env["ANTHROPIC_API_KEY"] = key
        }
        proc.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.appendLog(handle.availableData)
        }
        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            self?.appendLog(handle.availableData)
        }

        proc.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                self?.stdoutPipe?.fileHandleForReading.readabilityHandler = nil
                self?.stderrPipe?.fileHandleForReading.readabilityHandler = nil
                self?.stopPolling()
                self?.appendLogLine("[Process exited: \(p.terminationStatus)]")
                self?.state = .idle
                self?.process = nil
                self?.startedAt = nil
                self?.onStopped?()
                self?.onStopped = nil
            }
        }

        do {
            try proc.run()
            process = proc
            stdoutPipe = outPipe
            stderrPipe = errPipe
            state = .running(pid: proc.processIdentifier)
            startedAt = Date()
            startPolling()
        } catch {
            state = .failed("Failed to launch: \(error.localizedDescription)")
        }
    }

    func stop() {
        guard let proc = process, proc.isRunning else { return }
        stopPolling()
        // SIGINT first — matches main.py's _graceful_shutdown handler, which
        // tears down the tunnel/session cleanly. Escalate to SIGTERM only if
        // it doesn't exit in time.
        proc.interrupt()
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            if proc.isRunning {
                proc.terminate()
            }
        }
    }

    private func startPolling() {
        pollLastCount = 0
        nextCaptionID = 0
        captionLines = []
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while let self, !Task.isCancelled {
                await self.pollLatest()
                try? await Task.sleep(nanoseconds: 400_000_000)
            }
        }
    }

    private func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    @MainActor
    private func pollLatest() async {
        guard let url = URL(string: "http://localhost:8080/api/latest") else { return }
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let latest = try? JSONDecoder().decode(LatestResponse.self, from: data) else {
            return
        }

        // Mirrors control.html's startPoll(): the server keeps only a recent
        // window of lines, so track position via absolute start/total indices,
        // not lines.count. total < pollLastCount means the server restarted.
        if latest.total < pollLastCount {
            pollLastCount = latest.start
        }
        let skip = max(0, pollLastCount - latest.start)
        let freshLines = latest.lines.count > skip ? Array(latest.lines[skip...]) : []
        pollLastCount = latest.total
        guard !freshLines.isEmpty else { return }

        for raw in freshLines {
            nextCaptionID += 1
            captionLines.append(CaptionLine(id: nextCaptionID, kind: raw.kind, lang: raw.lang, text: raw.text))
        }
        if captionLines.count > 12 {
            captionLines.removeFirst(captionLines.count - 12)
        }
    }

    private func appendLog(_ data: Data) {
        guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
        DispatchQueue.main.async { [weak self] in
            self?.log += text
        }
    }

    private func appendLogLine(_ line: String) {
        log += line + "\n"
    }
}
