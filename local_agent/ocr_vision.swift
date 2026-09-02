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

func loadCGImage(path: String) -> CGImage? {
    let url = URL(fileURLWithPath: path)
    guard let image = NSImage(contentsOf: url) else { return nil }
    var rect = NSRect(origin: .zero, size: image.size)
    return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
}

func ocr(path: String) -> OCRImage {
    guard let cgImage = loadCGImage(path: path) else {
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

func crop(input: String, output: String, x: Double, y: Double, width: Double, height: Double) {
    guard let cgImage = loadCGImage(path: input) else {
        fputs("unreadable image\n", stderr)
        exit(1)
    }
    let pxWidth = Double(cgImage.width)
    let pxHeight = Double(cgImage.height)
    let cropX = max(0, x) * pxWidth
    let cropW = max(1, width) * pxWidth
    let cropH = max(1, height) * pxHeight
    let cropYFromTop = (1 - max(0, y) - max(0, height)) * pxHeight
    let rect = CGRect(
        x: cropX,
        y: max(0, cropYFromTop),
        width: min(cropW, pxWidth - cropX),
        height: min(cropH, pxHeight - max(0, cropYFromTop))
    )
    guard rect.width >= 1, rect.height >= 1, let sliced = cgImage.cropping(to: rect) else {
        fputs("empty crop\n", stderr)
        exit(1)
    }
    let rep = NSBitmapImageRep(cgImage: sliced)
    guard let png = rep.representation(using: .png, properties: [:]) else {
        fputs("png encode failed\n", stderr)
        exit(1)
    }
    do {
        try png.write(to: URL(fileURLWithPath: output))
    } catch {
        fputs("write failed: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
if args.first == "crop" {
    guard args.count == 7,
          let x = Double(args[3]),
          let y = Double(args[4]),
          let width = Double(args[5]),
          let height = Double(args[6]) else {
        fputs("usage: local-ocr crop INPUT OUTPUT X Y W H\n", stderr)
        exit(2)
    }
    crop(input: args[1], output: args[2], x: x, y: y, width: width, height: height)
    print("ok")
    exit(0)
}

var images: [OCRImage] = []
for path in args {
    images.append(ocr(path: path))
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
guard let data = try? encoder.encode(images), let json = String(data: data, encoding: .utf8) else {
    fputs("ocr json encode failed\n", stderr)
    exit(1)
}
print(json)
