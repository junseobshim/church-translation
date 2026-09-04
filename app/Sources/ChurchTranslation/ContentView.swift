import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @Environment(SessionController.self) private var session
    @State private var languages = LanguageSelection()
    @State private var devices: [SessionController.AudioDevice] = []
    @State private var selectedDeviceIndex: Int?
    @State private var isLoadingDevices = false
    @State private var outlineFileName: String?
    @State private var outlineTempPath: String?
    @State private var outlineError: String?
    @State private var showOutlineImporter = false
    @State private var tunnels: [SessionController.TunnelOption] = []
    @State private var selectedTunnel: String? // nil = off (local captions only)
    @State private var isLoadingTunnels = false

    private static let langNames = ["ko": "Korean", "en": "English", "es": "Spanish"]
    private static let captionURL = "http://localhost:8080"

    var body: some View {
        TabView {
            controlPanel
                .tabItem { Label("Control", systemImage: "slider.horizontal.3") }
            logPanel
                .tabItem { Label("Log", systemImage: "terminal") }
        }
        .frame(minWidth: 600)
        .navigationTitle("Church Translation Control Panel")
        .task {
            await loadDevices()
            await loadTunnels()
        }
    }

    private var controlPanel: some View {
        // ScrollView instead of a bare VStack: pins content to the top (no more
        // vertical centering when the window is taller than the content) and,
        // if content ever exceeds the window's height (e.g. a large crash
        // message), makes it scrollable instead of clipped.
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("Church Translation Control Panel")
                    .font(.headline)

                sourcePicker
                targetPicker
                devicePicker
                outlinePicker
                tunnelPicker

                HStack {
                    Button(isRunning ? "Stop" : "Start") {
                        if isRunning {
                            session.stop()
                        } else {
                            let device = selectedDeviceIndex.map(String.init) ?? ""
                            session.start(source: languages.source, target: languages.targetsCSV,
                                         device: device, outlinePath: outlineTempPath, tunnel: selectedTunnel)
                        }
                    }
                    .disabled(languages.targets.isEmpty)
                }

                statusLine
                if isRunning {
                    linksView
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollIndicators(.visible)
    }

    private var logPanel: some View {
        ScrollView {
            Text(session.log)
                .font(.system(.body, design: .monospaced))
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
                .padding(8)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .scrollIndicators(.visible)
    }

    private func loadDevices() async {
        isLoadingDevices = true
        let found = await session.listAudioDevices()
        devices = found
        if let selectedDeviceIndex, !found.contains(where: { $0.index == selectedDeviceIndex }) {
            self.selectedDeviceIndex = nil // previously-selected device unplugged
        }
        isLoadingDevices = false
    }

    private func loadTunnels() async {
        isLoadingTunnels = true
        let found = await session.listTunnels()
        tunnels = found
        // Default to Off (local only) — the operator opts into a tunnel
        // explicitly rather than it being picked automatically. If a
        // previously-selected tunnel disappears (list refreshed, no longer
        // runnable here), fall back to Off too, not to some other tunnel.
        if let selectedTunnel, !found.contains(where: { $0.name == selectedTunnel }) {
            self.selectedTunnel = nil
        }
        isLoadingTunnels = false
    }

    private var devicePicker: some View {
        HStack {
            Text("Device:").frame(width: 70, alignment: .leading)
            Picker("", selection: $selectedDeviceIndex) {
                Text("System Default").tag(Int?.none)
                ForEach(devices) { device in
                    Text(device.name).tag(Int?.some(device.index))
                }
            }
            .frame(maxWidth: 300)
            .disabled(isRunning || isLoadingDevices)

            Button(action: { Task { await loadDevices() } }) {
                Image(systemName: "arrow.clockwise")
            }
            .disabled(isRunning || isLoadingDevices)
        }
    }

    private var sourcePicker: some View {
        HStack {
            Text("Source:").frame(width: 70, alignment: .leading)
            Picker("", selection: Binding(
                get: { languages.source },
                set: { languages.selectSource($0) }
            )) {
                ForEach(LanguageSelection.sources, id: \.self) { lang in
                    Text(lang == "multi" ? "Multi (ko+en+es)" : (Self.langNames[lang] ?? lang)).tag(lang)
                }
            }
            .pickerStyle(.segmented)
            .disabled(isRunning)
        }
    }

    private var targetPicker: some View {
        HStack {
            Text("Targets:").frame(width: 70, alignment: .leading)
            ForEach(LanguageSelection.allTargets, id: \.self) { lang in
                Toggle(Self.langNames[lang] ?? lang, isOn: Binding(
                    get: { languages.targets.contains(lang) },
                    set: { _ in languages.toggleTarget(lang) }
                ))
                .toggleStyle(.button)
                .disabled(isRunning || languages.isTargetLocked || !languages.targetAllowed(lang))
            }
        }
    }

    private var isRunning: Bool {
        if case .running = session.state { return true }
        return false
    }

    @ViewBuilder
    private var statusLine: some View {
        switch session.state {
        case .idle:
            Text("Idle").foregroundStyle(.secondary)
        case .running(let pid):
            if let startedAt = session.startedAt {
                TimelineView(.periodic(from: startedAt, by: 1)) { context in
                    Text("Running (pid \(pid)) — \(elapsed(since: startedAt, to: context.date))")
                        .foregroundStyle(.green)
                }
            } else {
                Text("Running (pid \(pid))").foregroundStyle(.green)
            }
        case .failed(let message):
            Text(message)
                .foregroundStyle(.red)
                .frame(maxWidth: .infinity, alignment: .leading)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }

    private func elapsed(since start: Date, to now: Date) -> String {
        let seconds = max(0, Int(now.timeIntervalSince(start)))
        return String(format: "%02d:%02d", seconds / 60, seconds % 60)
    }

    private var linksView: some View {
        HStack(alignment: .top, spacing: 20) {
            linkBlock(url: Self.captionURL)
            if let name = selectedTunnel, let url = tunnels.first(where: { $0.name == name })?.url {
                linkBlock(url: url)
            }
        }
    }

    private func linkBlock(url: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(url)
                .font(.system(.body, design: .monospaced))
            Button("Copy Link") {
                let pasteboard = NSPasteboard.general
                pasteboard.clearContents()
                pasteboard.setString(url, forType: .string)
            }
        }
    }

    private var tunnelPicker: some View {
        HStack {
            Text("Tunnel:").frame(width: 70, alignment: .leading)
            Picker("", selection: $selectedTunnel) {
                Text("Off (local only)").tag(String?.none)
                ForEach(tunnels) { t in
                    Text(t.name).tag(String?.some(t.name))
                }
            }
            .frame(maxWidth: 260)
            .disabled(isRunning || isLoadingTunnels)

            Button(action: { Task { await loadTunnels() } }) {
                Image(systemName: "arrow.clockwise")
            }
            .disabled(isRunning || isLoadingTunnels)

            if tunnels.isEmpty, !isLoadingTunnels {
                Text("No runnable tunnels found on this Mac")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var outlinePicker: some View {
        HStack {
            Text("Outline:").frame(width: 70, alignment: .leading)
            Button(outlineFileName ?? "Choose .txt or .docx…") {
                showOutlineImporter = true
            }
            .disabled(isRunning)
            if outlineFileName != nil {
                Button(action: clearOutline) {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.plain)
                .disabled(isRunning)
            }
            if let outlineError {
                Text(outlineError)
                    .foregroundStyle(.red)
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .fileImporter(
            isPresented: $showOutlineImporter,
            allowedContentTypes: [.plainText, .init(filenameExtension: "docx")].compactMap { $0 }
        ) { result in
            Task { await handleOutlineImport(result) }
        }
    }

    private func clearOutline() {
        if let outlineTempPath {
            try? FileManager.default.removeItem(atPath: outlineTempPath)
        }
        outlineTempPath = nil
        outlineFileName = nil
        outlineError = nil
    }

    /// Both .txt and .docx end up as a plain UTF-8 temp file — main.py's
    /// --outline just reads a text file (doc §2.3: .docx goes through the
    /// bundled python-docx one-shot, no Swift-side XML parsing).
    private func handleOutlineImport(_ result: Result<URL, Error>) async {
        outlineError = nil
        guard case .success(let url) = result else { return }
        let accessed = url.startAccessingSecurityScopedResource()
        defer { if accessed { url.stopAccessingSecurityScopedResource() } }

        let text: String?
        if url.pathExtension.lowercased() == "docx" {
            text = await session.extractDocxText(at: url)
        } else {
            text = try? String(contentsOf: url, encoding: .utf8)
        }
        guard let text, !text.isEmpty else {
            outlineError = "Could not read outline text"
            return
        }

        let tempURL = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".txt")
        do {
            try text.write(to: tempURL, atomically: true, encoding: .utf8)
            clearOutline()
            outlineTempPath = tempURL.path
            outlineFileName = url.lastPathComponent
        } catch {
            outlineError = "Could not stage outline file"
        }
    }
}
