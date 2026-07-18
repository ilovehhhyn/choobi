import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct Raster {
    let width: Int
    let height: Int
    var rgba: [UInt8]
}

struct Box {
    var minX: Int
    var minY: Int
    var maxX: Int
    var maxY: Int

    var width: Int { maxX - minX + 1 }
    var height: Int { maxY - minY + 1 }
}

func loadPNG(_ path: String) throws -> Raster {
    let url = URL(fileURLWithPath: path) as CFURL
    guard let source = CGImageSourceCreateWithURL(url, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else { throw NSError(domain: "LineArt", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot decode \(path)"]) }

    let width = image.width
    let height = image.height
    var rgba = [UInt8](repeating: 0, count: width * height * 4)
    guard let context = CGContext(
        data: &rgba,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else { throw NSError(domain: "LineArt", code: 2, userInfo: [NSLocalizedDescriptionKey: "Cannot create bitmap context"]) }

    context.translateBy(x: 0, y: CGFloat(height))
    context.scaleBy(x: 1, y: -1)
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return Raster(width: width, height: height, rgba: rgba)
}

func writePNG(_ raster: Raster, to path: String) throws {
    let data = Data(raster.rgba) as CFData
    guard let provider = CGDataProvider(data: data),
          let image = CGImage(
              width: raster.width,
              height: raster.height,
              bitsPerComponent: 8,
              bitsPerPixel: 32,
              bytesPerRow: raster.width * 4,
              space: CGColorSpaceCreateDeviceRGB(),
              bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
              provider: provider,
              decode: nil,
              shouldInterpolate: false,
              intent: .defaultIntent
          )
    else { throw NSError(domain: "LineArt", code: 3, userInfo: [NSLocalizedDescriptionKey: "Cannot encode bitmap"]) }

    let url = URL(fileURLWithPath: path) as CFURL
    guard let destination = CGImageDestinationCreateWithURL(url, UTType.png.identifier as CFString, 1, nil)
    else { throw NSError(domain: "LineArt", code: 4, userInfo: [NSLocalizedDescriptionKey: "Cannot create PNG destination"]) }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination)
    else { throw NSError(domain: "LineArt", code: 5, userInfo: [NSLocalizedDescriptionKey: "Cannot write \(path)"]) }
}

func rotateCounterclockwise(_ source: Raster) -> Raster {
    var rotated = Raster(
        width: source.height,
        height: source.width,
        rgba: [UInt8](repeating: 0, count: source.width * source.height * 4)
    )
    for y in 0..<source.height {
        for x in 0..<source.width {
            let newX = y
            let newY = source.width - 1 - x
            let sourceIndex = (y * source.width + x) * 4
            let targetIndex = (newY * rotated.width + newX) * 4
            rotated.rgba[targetIndex..<(targetIndex + 4)] = source.rgba[sourceIndex..<(sourceIndex + 4)]
        }
    }
    return rotated
}

func luminance(_ raster: Raster, _ x: Int, _ y: Int) -> Int {
    let i = (y * raster.width + x) * 4
    return (54 * Int(raster.rgba[i]) + 183 * Int(raster.rgba[i + 1]) + 19 * Int(raster.rgba[i + 2])) >> 8
}

func dilate(_ mask: [UInt8], width: Int, height: Int, radius: Int) -> [UInt8] {
    let stride = width + 1
    var integral = [Int](repeating: 0, count: (width + 1) * (height + 1))
    for y in 0..<height {
        var rowSum = 0
        for x in 0..<width {
            rowSum += Int(mask[y * width + x])
            integral[(y + 1) * stride + x + 1] = integral[y * stride + x + 1] + rowSum
        }
    }

    var result = [UInt8](repeating: 0, count: width * height)
    for y in 0..<height {
        let y0 = max(0, y - radius)
        let y1 = min(height - 1, y + radius)
        for x in 0..<width {
            let x0 = max(0, x - radius)
            let x1 = min(width - 1, x + radius)
            let sum = integral[(y1 + 1) * stride + x1 + 1]
                - integral[y0 * stride + x1 + 1]
                - integral[(y1 + 1) * stride + x0]
                + integral[y0 * stride + x0]
            result[y * width + x] = sum > 0 ? 1 : 0
        }
    }
    return result
}

func connectedBoxes(_ mask: [UInt8], width: Int, height: Int) -> [Box] {
    var visited = [UInt8](repeating: 0, count: mask.count)
    var boxes: [Box] = []
    var queue: [Int] = []

    for start in mask.indices where mask[start] == 1 && visited[start] == 0 {
        visited[start] = 1
        queue.removeAll(keepingCapacity: true)
        queue.append(start)
        var head = 0
        var count = 0
        var box = Box(minX: width, minY: height, maxX: 0, maxY: 0)

        while head < queue.count {
            let current = queue[head]
            head += 1
            count += 1
            let x = current % width
            let y = current / width
            box.minX = min(box.minX, x)
            box.minY = min(box.minY, y)
            box.maxX = max(box.maxX, x)
            box.maxY = max(box.maxY, y)

            if x > 0 {
                let next = current - 1
                if mask[next] == 1 && visited[next] == 0 { visited[next] = 1; queue.append(next) }
            }
            if x + 1 < width {
                let next = current + 1
                if mask[next] == 1 && visited[next] == 0 { visited[next] = 1; queue.append(next) }
            }
            if y > 0 {
                let next = current - width
                if mask[next] == 1 && visited[next] == 0 { visited[next] = 1; queue.append(next) }
            }
            if y + 1 < height {
                let next = current + width
                if mask[next] == 1 && visited[next] == 0 { visited[next] = 1; queue.append(next) }
            }
        }

        let touchesEdge = box.minX < 8 || box.minY < 8 || box.maxX >= width - 8 || box.maxY >= height - 8
        let plausibleFace = count >= 700 && box.width >= 35 && box.height >= 35 && box.width <= 290 && box.height <= 250
        if plausibleFace && !touchesEdge { boxes.append(box) }
    }
    return boxes
}

func inkAlpha(for luminance: Int) -> UInt8 {
    guard luminance < 172 else { return 0 }
    let darkness = 172 - luminance
    return UInt8(min(255, 96 + darkness * 2))
}

func extractInk(_ source: Raster, box: Box, padding: Int = 8) -> Raster {
    let minX = max(0, box.minX - padding)
    let minY = max(0, box.minY - padding)
    let maxX = min(source.width - 1, box.maxX + padding)
    let maxY = min(source.height - 1, box.maxY + padding)
    let width = maxX - minX + 1
    let height = maxY - minY + 1
    var output = Raster(width: width, height: height, rgba: [UInt8](repeating: 0, count: width * height * 4))

    for y in 0..<height {
        for x in 0..<width {
            let alpha = inkAlpha(for: luminance(source, minX + x, minY + y))
            let i = (y * width + x) * 4
            output.rgba[i] = 0
            output.rgba[i + 1] = 0
            output.rgba[i + 2] = 0
            output.rgba[i + 3] = alpha
        }
    }
    return output
}

func tightInkBox(_ source: Raster, threshold: Int = 172) -> Box {
    var box = Box(minX: source.width, minY: source.height, maxX: 0, maxY: 0)
    for y in 0..<source.height {
        for x in 0..<source.width where luminance(source, x, y) < threshold {
            box.minX = min(box.minX, x)
            box.minY = min(box.minY, y)
            box.maxX = max(box.maxX, x)
            box.maxY = max(box.maxY, y)
        }
    }
    return box
}

let arguments = CommandLine.arguments
guard arguments.count == 4 else {
    FileHandle.standardError.write(Data("usage: line-art-extract FACE_SHEET NAME_IMAGE OUTPUT_DIR\n".utf8))
    exit(64)
}

let faceSource = try loadPNG(arguments[1])
let rotated = rotateCounterclockwise(faceSource)
var faceMask = [UInt8](repeating: 0, count: rotated.width * rotated.height)
for y in 0..<rotated.height {
    for x in 0..<rotated.width {
        faceMask[y * rotated.width + x] = luminance(rotated, x, y) < 150 ? 1 : 0
    }
}

let boxes = connectedBoxes(dilate(faceMask, width: rotated.width, height: rotated.height, radius: 6), width: rotated.width, height: rotated.height)
    .sorted { lhs, rhs in
        abs(lhs.minY - rhs.minY) > 35 ? lhs.minY < rhs.minY : lhs.minX < rhs.minX
    }

print("detected \(boxes.count) faces")
for (index, box) in boxes.enumerated() {
    print("face-\(index + 1): x=\(box.minX)...\(box.maxX) y=\(box.minY)...\(box.maxY) size=\(box.width)x\(box.height)")
}
guard boxes.count == 18 else {
    FileHandle.standardError.write(Data("Expected 18 face components, detected \(boxes.count)\n".utf8))
    exit(2)
}

let outputDirectory = URL(fileURLWithPath: arguments[3], isDirectory: true)
try FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true)
for (index, box) in boxes.enumerated() {
    try writePNG(extractInk(rotated, box: box), to: outputDirectory.appendingPathComponent("face-\(index + 1).png").path)
}

let nameSource = try loadPNG(arguments[2])
try writePNG(extractInk(nameSource, box: tightInkBox(nameSource), padding: 8), to: outputDirectory.appendingPathComponent("name.png").path)
