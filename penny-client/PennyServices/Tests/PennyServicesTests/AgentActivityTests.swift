import Foundation
import Testing
@testable import PennyServices

@Suite(.serialized)
@MainActor
struct AgentActivityTests {
    @Test(arguments: ["completed", "max_steps", "error", ""])
    func finishedForegroundActivityRetainsItsStepsToolsAndThinking(_ outcome: String) async {
        let (client, transport) = await activityClient()
        transport.seedRun("latest")
        transport.emit("thinking_updated", run: "latest", step: 1, thinking: "Compare both sources.\nThen explain. ✓")
        transport.emitTools(run: "latest")
        transport.emit("step_started", run: "latest", step: 2)
        transport.emit("thinking_updated", run: "latest", step: 2, thinking: "The sources agree.")
        let beforeFinish = client.foregroundProgress

        transport.emit("run_finished", run: "latest", outcome: outcome.isEmpty ? nil : outcome)
        transport.emitTyping(false)

        let retained = client.foregroundActivityRuns.first
        #expect(retained?.steps == beforeFinish?.steps)
        #expect(retained?.steps.count == 2)
        #expect(retained?.steps.first?.tools.map(\.name) == ["browse", "read_emails"])
        #expect(retained?.steps.first?.thinking == "Compare both sources.\nThen explain. ✓")
        #expect(retained?.steps.last?.thinking == "The sources agree.")
        #expect(retained?.isRunning == false)
        #expect(retained?.state == .finished(AgentProgressOutcome(rawValue: outcome)))
        #expect(client.foregroundProgress == nil)
        #expect(client.isTyping == false)
        client.disconnect()
    }

    @Test func successfulSendClearsPreviousActivityAndPreservesNewRunsAndBackgroundWork() async {
        let (client, transport) = await activityClient()
        transport.seedRun("previous")
        transport.emit("run_finished", run: "previous", outcome: "completed")
        transport.seedRun("background", scope: "background")
        transport.holdMessage = true

        client.sendMessage("Next question")
        await waitForActivity { transport.pendingMessage != nil }
        #expect(client.foregroundActivityRuns.map(\.id) == ["previous"])
        transport.seedRun("new-response")
        transport.completeMessage()
        await waitForActivity { client.foregroundActivityRuns.map(\.id) == ["new-response"] }

        #expect(client.foregroundProgress?.id == "new-response")
        #expect(client.backgroundProgressRuns.map(\.id) == ["background"])
        client.disconnect()
    }

    @Test func failedSendAndUnrelatedRequestsKeepPreviousActivity() async {
        let (client, transport) = await activityClient()
        transport.seedRun("previous")
        transport.emit("run_finished", run: "previous", outcome: "completed")
        let retained = client.foregroundActivityRuns
        client.requestConfig()
        await Task.yield()
        #expect(client.foregroundActivityRuns == retained)

        transport.failMessages = true
        client.sendMessage("Next question")
        await waitForActivity { client.lastError != nil }
        #expect(client.foregroundActivityRuns == retained)
        client.disconnect()
    }

    @Test func disconnectAndReconnectKeepCompletedAndInterruptedActivity() async {
        let (client, transport) = await activityClient()
        transport.seedRun("completed")
        transport.emit("run_finished", run: "completed", outcome: "completed")
        transport.seedRun("interrupted")
        transport.emit("thinking_updated", run: "interrupted", step: 1, thinking: "Check the source.")
        transport.seedRun("background", scope: "background")

        client.disconnect()
        #expect(client.foregroundActivityRuns.map(\.id) == ["completed", "interrupted"])
        #expect(client.foregroundActivityRuns.first?.state == .finished(.completed))
        #expect(client.foregroundActivityRuns.last?.state == .interrupted)
        #expect(client.foregroundActivityRuns.last?.steps.first?.thinking == "Check the source.")
        #expect(client.backgroundProgressRuns.isEmpty)
        #expect(client.foregroundProgress == nil)

        await client.connect()
        #expect(client.foregroundActivityRuns.count == 2)
        transport.emit("thinking_updated", run: "interrupted", step: 1, thinking: "The source is available.")
        #expect(client.foregroundProgress?.id == "interrupted")
        #expect(client.foregroundProgress?.steps.first?.thinking == "The source is available.")
        client.disconnect()
    }

    @Test func finishedBackgroundRunsAreRemovedAndLateUpdatesDoNotReviveFinishedForegroundRuns() async {
        let (client, transport) = await activityClient()
        transport.seedRun("background", scope: "background")
        transport.emit("run_finished", run: "background", scope: "background", outcome: "completed")
        #expect(client.agentProgressRuns.isEmpty)
        transport.seedRun("completed")
        transport.emit("run_finished", run: "completed", outcome: "completed")
        let retained = client.foregroundActivityRuns

        transport.emit("step_started", run: "completed", step: 2)
        transport.emit("thinking_updated", run: "completed", step: 1, thinking: "Late update")
        transport.emitTools(run: "completed")
        transport.emit("run_started", run: "completed")
        #expect(client.foregroundActivityRuns == retained)
        #expect(client.foregroundProgress == nil)
        client.disconnect()
    }
}

@MainActor
private func activityClient() async -> (PennyService, ActivityTransport) {
    let transport = ActivityTransport()
    let client = PennyService(databaseService: configuredDatabase(), prefs: configuredPrefs(), webSocketClient: transport)
    await client.connect()
    return (client, transport)
}

@MainActor
private func waitForActivity(_ condition: () -> Bool) async {
    for _ in 0..<100 {
        if condition() { return }
        try? await Task.sleep(for: .milliseconds(10))
    }
    #expect(condition())
}

@MainActor
private final class ActivityTransport: WebSocketTransport {
    var isConnected = false
    var holdMessage = false
    var failMessages = false
    var pendingMessage: CheckedContinuation<Void, Error>?
    private var onReceive: ReceiveHandler?

    func connect(request: URLRequest, onReceive: @escaping ReceiveHandler, onFailure: @escaping FailureHandler) {
        self.onReceive = onReceive
        isConnected = true
    }

    func disconnect() {
        onReceive = nil
        isConnected = false
        pendingMessage?.resume(throwing: ActivitySendError.failed)
        pendingMessage = nil
    }

    func send(_ data: Data) async throws {
        let payload = try JSONDecoder().decode([String: JSONValue].self, from: data)
        guard payload["type"] == .string("message") else { return }
        if failMessages { throw ActivitySendError.failed }
        if holdMessage {
            try await withCheckedThrowingContinuation { pendingMessage = $0 }
        }
    }

    func completeMessage() {
        pendingMessage?.resume()
        pendingMessage = nil
        holdMessage = false
    }

    func seedRun(_ run: String, scope: String = "foreground") {
        emit("run_started", run: run, scope: scope)
        emit("step_started", run: run, scope: scope, step: 1)
    }

    func emit(_ event: String, run: String, scope: String = "foreground", step: Int? = nil,
              thinking: String? = nil, outcome: String? = nil) {
        var payload: [String: Any] = [
            "type": "agent_progress", "event": event, "run_id": run,
            "agent": "chat", "scope": scope, "tools": []
        ]
        if let step { payload["step"] = step }
        if let thinking { payload["thinking"] = thinking }
        if let outcome { payload["outcome"] = outcome }
        do {
            onReceive?(try JSONSerialization.data(withJSONObject: payload))
        } catch {
            Issue.record(error)
        }
    }

    func emitTools(run: String) {
        onReceive?(Data("""
        {"type":"agent_progress","event":"tools_started","run_id":"\(run)","agent":"chat","scope":"foreground","step":1,"tools":[{"name":"browse","arguments":{}},{"name":"read_emails","arguments":{}}]}
        """.utf8))
    }

    func emitTyping(_ active: Bool) {
        onReceive?(Data("{\"type\":\"typing\",\"active\":\(active)}".utf8))
    }
}

private enum ActivitySendError: Error {
    case failed
}
