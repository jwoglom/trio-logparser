import Foundation

// stdin: "<yyyy-MM-dd HH:mm:ss.SSSSSS>\t<cargo hex>" per line; stdout: TandemKit HistoryLogExport JSONL.
let tsFormatter = DateFormatter()
tsFormatter.locale = Locale(identifier: "en_US_POSIX")
tsFormatter.dateFormat = "yyyy-MM-dd HH:mm:ss.SSSSSS"

func hexData(_ s: Substring) -> Data? {
    var data = Data(capacity: s.count / 2)
    var i = s.startIndex
    while i < s.endIndex {
        let j = s.index(i, offsetBy: 2, limitedBy: s.endIndex) ?? s.endIndex
        guard let b = UInt8(s[i..<j], radix: 16) else { return nil }
        data.append(b)
        i = j
    }
    return data
}

var bad = 0
while let line = readLine() {
    let parts = line.split(separator: "\t", maxSplits: 1)
    guard parts.count == 2, let date = tsFormatter.date(from: String(parts[0])), let cargo = hexData(parts[1]) else {
        bad += 1; continue
    }
    let message = HistoryLogStreamResponse(cargo: cargo)
    if let out = HistoryLogExport.jsonlLine(for: message, receivedAt: date) {
        print(out)
    } else {
        bad += 1
    }
}
if bad > 0 { FileHandle.standardError.write("skipped \(bad) unparseable line(s)\n".data(using: .utf8)!) }
