import Observation

/// Source/target language selection, porting control.html's exact validation
/// rules (selectSource/targetAllowed/toggle in control.html) so the SwiftUI
/// picker and the CLI (main.py's `_parse_and_validate_targets`) never disagree
/// about what's a legal combination.
@Observable
final class LanguageSelection {
    static let sources = ["ko", "en", "es", "multi"]
    static let allTargets = ["ko", "en", "es"]

    private(set) var source: String = "ko"
    /// Ordered, not a Set — mirrors control.html's insertion-order Set so the
    /// first target (the web viewer's default) is predictable.
    private(set) var targets: [String] = ["en"]

    var isTargetLocked: Bool { source == "multi" }

    /// English-only source can't target English (English → English does
    /// nothing); every other (source, target) pair is selectable, including
    /// same-language passthrough for bilingual sources (ko+en, es+en).
    func targetAllowed(_ lang: String) -> Bool {
        !(source == "en" && lang == "en")
    }

    func selectSource(_ newSource: String) {
        guard Self.sources.contains(newSource) else { return }
        source = newSource

        if newSource == "multi" {
            // Multi always translates into all three; target selection locks.
            targets = Self.allTargets
            return
        }

        // Keep current targets across a source switch; drop "en" if the new
        // source is English-only, then ensure at least one target remains.
        if newSource == "en" {
            targets.removeAll { $0 == "en" }
        }
        if targets.isEmpty {
            targets = [newSource == "en" ? "ko" : "en"]
        }
    }

    func toggleTarget(_ lang: String) {
        guard source != "multi" else { return }
        guard targetAllowed(lang) else { return }
        if targets.contains(lang) {
            guard targets.count > 1 else { return } // keep at least one target
            targets.removeAll { $0 == lang }
        } else {
            targets.append(lang)
        }
    }

    var targetsCSV: String { targets.joined(separator: ",") }
}
