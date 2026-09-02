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

struct DiffRegion: Codable {
    let x: Float
    let y: Float
    let width: Float
    let height: Float
    let score: Float
}

struct DiffResult: Codable {
    let width: Int
    let height: Int
    let changedRatio: Float
    let regions: [DiffRegion]
    let error: String?
}

func rgbaBytes(_ image: CGImage, width: Int, height: Int) -> [UInt8]? {
    guard let ctx = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { return nil }
    ctx.interpolationQuality = .low
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let pointer = ctx.data else { return nil }
    let count = width * height * 4
    return Array(UnsafeBufferPointer(start: pointer.assumingMemoryBound(to: UInt8.self), count: count))
}

func pixelDiff(leftPath: String, rightPath: String, threshold: Float) -> DiffResult {
    guard let left = loadCGImage(path: leftPath), let right = loadCGImage(path: rightPath) else {
        return DiffResult(width: 0, height: 0, changedRatio: 0, regions: [], error: "unreadable image")
    }
    let width = min(left.width, right.width)
    let height = min(left.height, right.height)
    guard let leftBytes = rgbaBytes(left, width: width, height: height),
          let rightBytes = rgbaBytes(right, width: width, height: height) else {
        return DiffResult(width: width, height: height, changedRatio: 0, regions: [], error: "pixel read failed")
    }
    let grid = 24
    let cellW = max(1, width / grid)
    let cellH = max(1, height / grid)
    var dirty = Array(repeating: Array(repeating: Float(0), count: grid), count: grid)
    var dirtyCount = 0
    for row in 0..<grid {
        for col in 0..<grid {
            let x0 = col * cellW
            let y0 = row * cellH
            let x1 = min(width, x0 + cellW)
            let y1 = min(height, y0 + cellH)
            var acc: Float = 0
            var n: Float = 0
            var y = y0
            while y < y1 {
                var x = x0
                while x < x1 {
                    let i = (y * width + x) * 4
                    let dr = abs(Int(leftBytes[i]) - Int(rightBytes[i]))
                    let dg = abs(Int(leftBytes[i + 1]) - Int(rightBytes[i + 1]))
                    let db = abs(Int(leftBytes[i + 2]) - Int(rightBytes[i + 2]))
                    acc += Float(max(dr, max(dg, db))) / 255.0
                    n += 1
                    x += 2
                }
                y += 2
            }
            let score = n > 0 ? acc / n : 0
            if score >= threshold {
                dirty[row][col] = score
                dirtyCount += 1
            }
        }
    }
    var regions: [DiffRegion] = []
    var seen = Array(repeating: Array(repeating: false, count: grid), count: grid)
    for row in 0..<grid {
        for col in 0..<grid {
            if dirty[row][col] < threshold || seen[row][col] { continue }
            var minC = col, maxC = col, minR = row, maxR = row
            var peak = dirty[row][col]
            var stack = [(row, col)]
            seen[row][col] = true
            while let (r, c) = stack.popLast() {
                peak = max(peak, dirty[r][c])
                minC = min(minC, c); maxC = max(maxC, c)
                minR = min(minR, r); maxR = max(maxR, r)
                for (nr, nc) in [(r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)] {
                    if nr >= 0 && nr < grid && nc >= 0 && nc < grid && !seen[nr][nc] && dirty[nr][nc] >= threshold {
                        seen[nr][nc] = true
                        stack.append((nr, nc))
                    }
                }
            }
            let cellWf = 1.0 / Float(grid)
            let cellHf = 1.0 / Float(grid)
            let x = Float(minC) * cellWf
            let widthN = Float(maxC - minC + 1) * cellWf
            let heightN = Float(maxR - minR + 1) * cellHf
            let yFromTop = Float(minR) * cellHf
            let y = max(0, 1 - yFromTop - heightN)
            regions.append(DiffRegion(x: x, y: y, width: widthN, height: heightN, score: peak))
        }
    }
    regions.sort { $0.score > $1.score }
    if regions.count > 8 { regions = Array(regions.prefix(8)) }
    let ratio = Float(dirtyCount) / Float(grid * grid)
    return DiffResult(width: width, height: height, changedRatio: ratio, regions: regions, error: nil)
}

if args.first == "diff" {
    guard args.count >= 3 else {
        fputs("usage: local-ocr diff LEFT RIGHT [threshold]\n", stderr)
        exit(2)
    }
    let threshold = args.count >= 4 ? (Float(args[3]) ?? 0.12) : 0.12
    let result = pixelDiff(leftPath: args[1], rightPath: args[2], threshold: threshold)
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    guard let data = try? encoder.encode(result), let json = String(data: data, encoding: .utf8) else {
        fputs("diff json encode failed\n", stderr)
        exit(1)
    }
    print(json)
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
