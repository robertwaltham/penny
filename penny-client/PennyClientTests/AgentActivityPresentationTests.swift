import Testing
@testable import PennyClient
@testable import PennyServices

struct AgentActivityPresentationTests {
    @Test func completedAndInterruptedRunsHaveAccurateStatusLabels() {
        var run = AgentProgressRunItem(id: "display", agent: "chat", scope: .foreground)
        #expect(run.activityStatus == "Working")
        run.state = .finished(.completed)
        #expect(run.activityStatus == "Completed")
        run.state = .finished(.maxSteps)
        #expect(run.activityStatus == "Step limit reached")
        run.state = .finished(.error)
        #expect(run.activityStatus == "Run failed")
        run.state = .finished(nil)
        #expect(run.activityStatus == "Finished")
        run.state = .interrupted
        #expect(run.activityStatus == "Connection interrupted")
    }

    @Test func stepAndToolLabelsPreserveAvailableDetails() {
        #expect(AgentProgressStepItem(number: 2, maxSteps: 4).activityTitle == "Step 2 of 4")
        #expect(AgentProgressStepItem(number: 2).activityTitle == "Step 2")
        let read = AgentProgressToolItem(name: "browse", arguments: ["queries": .array([.string("https://www.example.com/page")])])
        let search = AgentProgressToolItem(name: "browse", arguments: ["queries": .array([.string("a sample query")])])
        #expect(read.activityLabel == "Reading example.com")
        #expect(search.activityLabel == "Searching \"a sample query\"")
        #expect(AgentProgressToolItem(name: "read_emails").activityLabel == "Reading emails")
        #expect(AgentProgressToolItem(name: "custom_tool").activityLabel == "Using custom_tool")
    }
}
