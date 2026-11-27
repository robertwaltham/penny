import PennyDesign
import XCTest

final class ImageAttachmentSettingsUITests: XCTestCase {
    func testScheduledReportEditingCanBeTurnedOnAndOffWhenConfigured() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchStory("settingsImageEditing")
        let toggle = editingSwitch(in: app)
        XCTAssertTrue(toggle.waitForExistence(timeout: UITestWait.standard))
        XCTAssertTrue(toggle.isEnabled)
        XCTAssertEqual(toggle.value as? String, "0")

        // SwiftUI exposes the labeled row and its tappable switch separately.
        let control = toggle.switches.firstMatch
        XCTAssertTrue(control.isHittable)
        control.tap()
        XCTAssertTrue(waitForValue(toggle, toEqual: "1"))
        let saved = XCTNSPredicateExpectation(predicate: NSPredicate(format: "enabled == 1"), object: toggle)
        XCTAssertEqual(XCTWaiter().wait(for: [saved], timeout: UITestWait.standard), .completed)
        capture("scheduled-report-image-editing-enabled")

        control.tap()
        XCTAssertTrue(waitForValue(toggle, toEqual: "0"))
    }

    func testScheduledReportEditingIsHiddenWithoutWorkflow() {
        continueAfterFailure = false
        let app = XCUIApplication()
        app.launchStory("settingsImageEditingUnavailable")
        let automatic = app.switches[AXID.imageAttachmentSetting("IOS_AUTOMATIC_IMAGES")]
        let loaded = XCTNSPredicateExpectation(predicate: NSPredicate(format: "exists == 1 AND enabled == 1"), object: automatic)
        XCTAssertEqual(XCTWaiter().wait(for: [loaded], timeout: UITestWait.standard), .completed)
        XCTAssertFalse(editingSwitch(in: app).exists)
        capture("scheduled-report-image-editing-unavailable")
    }

    private func editingSwitch(in app: XCUIApplication) -> XCUIElement {
        app.switches[AXID.imageAttachmentSetting("SCHEDULED_REPORT_IMAGE_EDITING")]
    }

    private func capture(_ name: String) {
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }
}
