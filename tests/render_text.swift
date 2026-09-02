import AppKit
import Foundation

guard CommandLine.arguments.count >= 3 else {
    fputs("usage: render_text TEXT OUTPUT.png\n", stderr)
    exit(2)
}

let text = CommandLine.arguments[1]
let output = CommandLine.arguments[2]
let size = NSSize(width: 900, height: 220)
let image = NSImage(size: size)
image.lockFocus()
NSColor.white.setFill()
NSRect(origin: .zero, size: size).fill()
let attrs: [NSAttributedString.Key: Any] = [
    .font: NSFont.systemFont(ofSize: 36),
    .foregroundColor: NSColor.black,
]
(text as NSString).draw(at: NSPoint(x: 24, y: 90), withAttributes: attrs)
image.unlockFocus()

guard let tiff = image.tiffRepresentation,
      let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else {
    fputs("png encode failed\n", stderr)
    exit(1)
}

do {
    try png.write(to: URL(fileURLWithPath: output))
} catch {
    fputs("write failed: \(error.localizedDescription)\n", stderr)
    exit(1)
}
