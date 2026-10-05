import AppKit
import Foundation

// Native CrossOver raw menu entry. Python remains the single owner of the
// existing runtime safety checks and flock; Terminal is never opened.
struct LaunchConfiguration: Decodable {
    let python: String
    let script: String
    let lab: String
    let environment: [String: String]
}

func alert(_ title: String, _ message: String, critical: Bool) {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    application.activate(ignoringOtherApps: true)
    let dialog = NSAlert()
    dialog.messageText = title
    dialog.informativeText = message
    dialog.alertStyle = critical ? .critical : .informational
    dialog.addButton(withTitle: "OK")
    dialog.runModal()
}

do {
    guard CommandLine.arguments.count == 3 else {
        throw NSError(domain: "DXMT", code: 1, userInfo: [NSLocalizedDescriptionKey: "Не указана конфигурация запуска."])
    }
    let config = try JSONDecoder().decode(LaunchConfiguration.self,
        from: Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1])))
    let action = CommandLine.arguments[2]
    guard ["baseline", "experiment", "stop", "check", "reports"].contains(action) else {
        throw NSError(domain: "DXMT", code: 2, userInfo: [NSLocalizedDescriptionKey: "Неизвестный вариант запуска."])
    }
    let directory = URL(fileURLWithPath: config.lab).appendingPathComponent("reports")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700])
    let logURL = directory.appendingPathComponent("gui-\(action)-\(Int(Date().timeIntervalSince1970))-\(ProcessInfo.processInfo.processIdentifier).log")
    FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
    let output = try FileHandle(forWritingTo: logURL)
    let process = Process()
    process.executableURL = URL(fileURLWithPath: config.python)
    process.arguments = [config.script, "entry", action]
    process.environment = ProcessInfo.processInfo.environment.merging(config.environment) { _, new in new }
    process.environment?["PYTHONUNBUFFERED"] = "1"
    process.standardOutput = output
    process.standardError = output
    try process.run()
    process.waitUntilExit()
    try output.close()
    if process.terminationStatus != 0 {
        let tail = (try? String(contentsOf: logURL, encoding: .utf8))?.split(separator: "\n").suffix(8).joined(separator: "\n") ?? ""
        alert("DXMT: запуск остановлен", "\(tail)\n\nЖурнал: \(logURL.path)", critical: true)
    } else if action == "check" {
        alert("DXMT готов", "Baseline и experiment проверены. Запускай нужную версию из CrossOver. Перед переключением закрой игру и Windows Steam или нажми «Остановить тест».", critical: false)
    }
    exit(process.terminationStatus)
} catch {
    alert("DXMT: ошибка", error.localizedDescription, critical: true)
    exit(1)
}
