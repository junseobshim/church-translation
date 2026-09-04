import Foundation

/// A proper-noun translation hint a church can add without a code change —
/// main.py's CUSTOM_TERMS env var (see main.py's _parse_custom_terms/
/// _term_prefs). Any subset of languages may be filled in per term; an empty
/// language just means "let Claude translate it naturally" for that target.
struct CustomTerm: Identifiable, Codable, Equatable {
    var id: UUID = UUID()
    var ko: String = ""
    var en: String = ""
    var es: String = ""

    var isEmpty: Bool { ko.isEmpty && en.isEmpty && es.isEmpty }
}

/// Local (non-secret) persistence — UserDefaults, not Keychain. Encodes to/
/// from the exact JSON shape main.py's CUSTOM_TERMS parser expects.
enum CustomTermsStore {
    private static let key = "customTerms"

    static func load() -> [CustomTerm] {
        guard let data = UserDefaults.standard.data(forKey: key),
              let terms = try? JSONDecoder().decode([CustomTerm].self, from: data) else {
            return []
        }
        return terms
    }

    static func save(_ terms: [CustomTerm]) {
        let nonEmpty = terms.filter { !$0.isEmpty }
        guard let data = try? JSONEncoder().encode(nonEmpty) else { return }
        UserDefaults.standard.set(data, forKey: key)
    }

    /// What SessionController injects as CUSTOM_TERMS — main.py only needs
    /// ko/en/es per entry, not the Swift-side id.
    static func envJSON() -> String? {
        let terms = load().filter { !$0.isEmpty }
        guard !terms.isEmpty else { return nil }
        let plain = terms.map { ["ko": $0.ko, "en": $0.en, "es": $0.es] }
        guard let data = try? JSONSerialization.data(withJSONObject: plain) else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
