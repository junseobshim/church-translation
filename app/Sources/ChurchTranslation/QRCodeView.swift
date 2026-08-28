import SwiftUI
import AppKit
import CoreImage.CIFilterBuiltins

/// QR code for the caption display URL (doc §2.3: "Links/QRs for the display
/// URLs"). Pure CoreImage — no third-party dependency.
struct QRCodeView: View {
    let string: String

    var body: some View {
        if let image = Self.generate(from: string) {
            Image(nsImage: image)
                .interpolation(.none)
                .resizable()
                .frame(width: 96, height: 96)
        } else {
            Color.clear.frame(width: 96, height: 96)
        }
    }

    private static func generate(from string: String) -> NSImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(string.utf8)
        filter.correctionLevel = "M"
        guard let output = filter.outputImage else { return nil }
        let scaled = output.transformed(by: CGAffineTransform(scaleX: 8, y: 8))
        let rep = NSCIImageRep(ciImage: scaled)
        let image = NSImage(size: rep.size)
        image.addRepresentation(rep)
        return image
    }
}
