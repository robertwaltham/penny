import Foundation

public struct AgentProgressToolItem: Identifiable, Equatable {
    public let id = UUID()
    public let name: String
    public let arguments: [String: AgentProgressValue]

    public init(name: String, arguments: [String: AgentProgressValue] = [:]) {
        self.name = name
        self.arguments = arguments
    }
}

public struct AgentProgressStepItem: Identifiable, Equatable {
    public let id = UUID()
    public let number: Int
    public let maxSteps: Int?
    public var tools: [AgentProgressToolItem]
    public var thinking: String?

    public init(number: Int, maxSteps: Int? = nil, tools: [AgentProgressToolItem] = [], thinking: String? = nil) {
        self.number = number
        self.maxSteps = maxSteps
        self.tools = tools
        self.thinking = thinking
    }
}

public enum AgentProgressRunState: Equatable {
    case running
    case finished(AgentProgressOutcome?)
    case interrupted
}

public struct AgentProgressRunItem: Identifiable, Equatable {
    public let id: String
    public let agent: String
    public let scope: AgentProgressScope
    public let startedAt: Date
    public var steps: [AgentProgressStepItem]
    public var state: AgentProgressRunState

    public var isRunning: Bool { state == .running }

    public init(
        id: String,
        agent: String,
        scope: AgentProgressScope,
        startedAt: Date = .now,
        steps: [AgentProgressStepItem] = [],
        state: AgentProgressRunState = .running
    ) {
        self.id = id
        self.agent = agent
        self.scope = scope
        self.startedAt = startedAt
        self.steps = steps
        self.state = state
    }
}
