import SwiftUI

/// Settings scene (Cmd+,): API keys (Keychain) and Custom Terms (local,
/// non-secret) as separate tabs — the terms table wants more room than the
/// key form.
struct SettingsView: View {
    var body: some View {
        TabView {
            APIKeysSettingsView()
                .tabItem { Label("API Keys", systemImage: "key") }
            CustomTermsSettingsView()
                .tabItem { Label("Custom Terms", systemImage: "textformat") }
        }
        .frame(minWidth: 520, idealWidth: 560, minHeight: 400, idealHeight: 440)
    }
}

/// API keys move from .env to Keychain (docs/native-macos-app-migration.md
/// §2.3/§2.4).
private struct APIKeysSettingsView: View {
    @State private var sonioxKey: String = KeychainStore.get("SONIOX_API_KEY") ?? ""
    @State private var anthropicKey: String = KeychainStore.get("ANTHROPIC_API_KEY") ?? ""
    @State private var saved = false
    @State private var importMessage: String?

    var body: some View {
        Form {
            SecureField("Soniox API Key", text: $sonioxKey)
            SecureField("Anthropic API Key", text: $anthropicKey)

            HStack {
                Button("Save") {
                    KeychainStore.set(sonioxKey, for: "SONIOX_API_KEY")
                    KeychainStore.set(anthropicKey, for: "ANTHROPIC_API_KEY")
                    saved = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { saved = false }
                }
                if saved {
                    Text("Saved").foregroundStyle(.secondary)
                }
            }

            Text("Leave blank to keep using .env (repo checkout dev mode only).")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if EnvImporter.legacyEnvExists {
                Divider()
                Button("Import from .env") {
                    let values = EnvImporter.parse(path: EnvImporter.legacyEnvPath)
                    var imported = 0
                    if let v = values["SONIOX_API_KEY"], !v.isEmpty { sonioxKey = v; imported += 1 }
                    if let v = values["ANTHROPIC_API_KEY"], !v.isEmpty { anthropicKey = v; imported += 1 }
                    importMessage = imported > 0
                        ? "Filled \(imported) key(s) from .env — click Save above to store in Keychain."
                        : "No SONIOX_API_KEY/ANTHROPIC_API_KEY found in .env."
                }
                if let importMessage {
                    Text(importMessage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(20)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

/// Table of extra proper-noun translation hints (main.py's CUSTOM_TERMS) —
/// add/remove rows freely, any subset of the three languages per row. Saved
/// to UserDefaults on every change; no separate Save step, unlike API keys.
private struct CustomTermsSettingsView: View {
    @State private var terms: [CustomTerm] = CustomTermsStore.load()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Extra names or terms that shouldn't be translated literally — ministries, staff, buildings. Leave a language blank to translate it naturally there.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                Text("Korean").frame(width: 120, alignment: .leading)
                Text("English").frame(width: 120, alignment: .leading)
                Text("Spanish").frame(width: 120, alignment: .leading)
            }
            .font(.caption.bold())

            ScrollView {
                VStack(spacing: 4) {
                    ForEach($terms) { $term in
                        HStack {
                            TextField("", text: $term.ko).frame(width: 120)
                            TextField("", text: $term.en).frame(width: 120)
                            TextField("", text: $term.es).frame(width: 120)
                            Button(action: { remove(term) }) {
                                Image(systemName: "trash")
                            }
                            .buttonStyle(.plain)
                        }
                        // Reserves room for macOS's overlay scrollbar so it
                        // doesn't sit on top of the trash button — without
                        // this you have to wait for the indicator to fade
                        // before you can click it.
                        .padding(.trailing, 16)
                    }
                }
            }
            .scrollIndicators(.visible)

            Button("Add Term") {
                terms.append(CustomTerm())
            }
        }
        .padding(20)
        .onChange(of: terms) { _, newValue in
            CustomTermsStore.save(newValue)
        }
    }

    private func remove(_ term: CustomTerm) {
        terms.removeAll { $0.id == term.id }
    }
}
