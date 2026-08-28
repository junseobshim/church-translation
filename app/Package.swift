// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ChurchTranslation",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "ChurchTranslation",
            path: "Sources/ChurchTranslation"
        )
    ]
)
