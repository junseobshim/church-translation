import Foundation

/// First-run .env import (doc §2.4/Phase 3): a volunteer upgrading from the
/// old git-clone workflow already has working keys in
/// ~/Documents/church-translation/.env — offer to bring them into Keychain
/// instead of retyping. Deliberately only fills the Settings fields for
/// review; saving to Keychain still requires an explicit Save click.
enum EnvImporter {
    static let legacyEnvPath = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Documents/church-translation/.env")

    static var legacyEnvExists: Bool {
        FileManager.default.fileExists(atPath: legacyEnvPath.path)
    }

    /// Simple KEY=VALUE line parser — no quoting/escaping support, matching
    /// this repo's actual .env format. Ignores blank lines and comments.
    static func parse(path: URL) -> [String: String] {
        guard let text = try? String(contentsOf: path, encoding: .utf8) else { return [:] }
        var result: [String: String] = [:]
        for line in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, !trimmed.hasPrefix("#"),
                  let eq = trimmed.firstIndex(of: "=") else { continue }
            let key = trimmed[trimmed.startIndex..<eq].trimmingCharacters(in: .whitespaces)
            let value = trimmed[trimmed.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            result[key] = value
        }
        return result
    }
}
