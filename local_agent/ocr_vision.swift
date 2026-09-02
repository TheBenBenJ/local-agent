import AppKit
import Foundation
import Vision

struct OCRLine: Codable {
    let text: String
    let confidence: Float
    let x: Float
    let y: Float
    let width: Float
    let height: Float
}

struct OCRImage: Codable {
    let path: String
    let lines: [OCRLine]
    let error: String?
}

func ocr(path: String) -> OCRImage {
    let url = URL(fileURLWithPath: path)
    guard let image = NSImage(contentsOf: url) else {
        return OCRImage(path: path, lines: [], error: "unreadable image")
    }
    var rect = NSRect(origin: .zero, size: image.size)
    guard let cgImage = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else {
        return OCRImage(path: path, lines: [], error: "unreadable image")
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["fr-FR", "en-US"]

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    do {
        try handler.perform([request])
    } catch {
        request.recognitionLanguages = []
        do {
            try handler.perform([request])
        } catch {
            return OCRImage(path: path, lines: [], error: error.localizedDescription)
        }
    }

    var lines: [OCRLine] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        if candidate.confidence < 0.3 { continue }
        let box = observation.boundingBox
        lines.append(OCRLine(
            text: candidate.string,
            confidence: candidate.confidence,
            x: Float(box.origin.x),
            y: Float(box.origin.y),
            width: Float(box.size.width),
            height: Float(box.size.height)
        ))
    }
    lines.sort { first, second in
        if abs(first.y - second.y) > 0.015 { return first.y > second.y }
        return first.x < second.x
    }
    return OCRImage(path: path, lines: lines, error: nil)
}

var images: [OCRImage] = []
for path in CommandLine.arguments.dropFirst() {
    images.append(ocr(path: path))
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
guard let data = try? encoder.encode(images), let json = String(data: data, encoding: .utf8) else {
    fputs("ocr json encode failed\n", stderr)
    exit(1)
}
print(json)
