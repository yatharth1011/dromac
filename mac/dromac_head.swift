// Head tracking for Dromac's 8D: watches your face through the Mac's camera
// and prints which way your head is turned, so the sound stays put in the room
// while you turn (look left, and what was in front is now on your right).
// Built by server.py on first use: swiftc -O dromac_head.swift -o dromac-head
//
// Output, ~30 lines a second: "yaw pitch shape age" -- yaw in radians, > 0 turned
// to your left; pitch, Vision's own estimate (weak); shape, the nose's height
// between eyes and mouth (eyes 0, mouth 1), which drops as you look up and
// rises as you look down -- the effects process turns that into the real
// pitch, measured against your own resting face; age, how many seconds old
// the frame is. Or "none" with no face.
// Frames are looked at and dropped: nothing is saved or sent anywhere. The
// camera is on only while this runs (macOS shows its green light).
import AVFoundation
import Foundation
import Vision

setvbuf(stdout, nil, _IOLBF, 0)

final class Tracker: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let queue = DispatchQueue(label: "dromac.head")
    // two passes on each frame: the face (its yaw comes continuous from this
    // one) and then its landmarks, for looking up/down -- the landmarks
    // request's own yaw only comes in coarse steps
    let request = VNDetectFaceRectanglesRequest()
    let landmarks = VNDetectFaceLandmarksRequest()
    var lastSeen = Date.distantPast
    var saidNone = false

    func start() {
        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .front)
                ?? AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: camera) else {
            FileHandle.standardError.write("dromac-head: no camera\n".data(using: .utf8)!)
            exit(2)
        }
        request.revision = VNDetectFaceRectanglesRequestRevision3   // continuous yaw, pitch
        landmarks.revision = VNDetectFaceLandmarksRequestRevision3
        session.beginConfiguration()
        session.sessionPreset = .vga640x480                          // plenty for a face; light on the CPU
        session.addInput(input)
        let output = AVCaptureVideoDataOutput()
        output.alwaysDiscardsLateVideoFrames = true
        output.setSampleBufferDelegate(self, queue: queue)
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        FileHandle.standardError.write("ready \(camera.localizedName)\n".data(using: .utf8)!)
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pixels = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        let handler = VNImageRequestHandler(cvPixelBuffer: pixels, orientation: .up)
        try? handler.perform([request])
        // the biggest face is the listener's
        let face = (request.results ?? []).max { $0.boundingBox.width < $1.boundingBox.width }
        var shape = -1.0
        if let f = face {
            landmarks.inputFaceObservations = [f]        // landmarks for this face, no second search
            try? handler.perform([landmarks])
            if let lf = landmarks.results?.first { shape = noseShape(lf) }
        }
        if let f = face, let yaw = f.yaw?.doubleValue {
            // Vision's yaw, as it comes, already has your left turn positive
            // (checked by ear: negating it put things on the wrong side).
            let pitch = f.pitch?.doubleValue ?? 0
            // and how old this frame already is (capture -> now), so the
            // prediction downstream knows exactly how far to look ahead
            let clock = session.synchronizationClock ?? CMClockGetHostTimeClock()
            let age = CMTimeGetSeconds(CMTimeSubtract(CMClockGetTime(clock), CMSampleBufferGetPresentationTimeStamp(sampleBuffer)))
            print(String(format: "%.4f %.4f %.4f %.4f", yaw, pitch, shape, min(max(age, 0), 0.5)))
            lastSeen = Date()
            saidNone = false
        } else if Date().timeIntervalSince(lastSeen) > 0.5 && !saidNone {
            print("none")
            saidNone = true
        }
    }
}

// Where the nose tip sits between the eyes (0) and the mouth (1), in the
// face's own frame. Looking up foreshortens the face so the nose rides up
// toward the eyes; looking down, it drops toward the mouth.
func noseShape(_ f: VNFaceObservation) -> Double {
    guard let lm = f.landmarks, let le = lm.leftEye?.normalizedPoints, let re = lm.rightEye?.normalizedPoints,
          let nose = lm.nose?.normalizedPoints, let lips = lm.outerLips?.normalizedPoints,
          !le.isEmpty, !re.isEmpty, !nose.isEmpty, !lips.isEmpty else { return -1 }
    let eyes = (le + re).map { Double($0.y) }.reduce(0, +) / Double(le.count + re.count)
    let tip = nose.map { Double($0.y) }.min()!                  // y points up: the tip is the lowest
    let mouth = lips.map { Double($0.y) }.reduce(0, +) / Double(lips.count)
    let span = eyes - mouth
    return span > 0.01 ? (eyes - tip) / span : -1
}

let tracker = Tracker()
switch AVCaptureDevice.authorizationStatus(for: .video) {
case .authorized:
    tracker.start()
case .notDetermined:
    AVCaptureDevice.requestAccess(for: .video) { ok in
        if ok { tracker.start() } else { FileHandle.standardError.write("dromac-head: camera not allowed\n".data(using: .utf8)!); exit(3) }
    }
default:
    FileHandle.standardError.write("dromac-head: camera not allowed (System Settings > Privacy & Security > Camera)\n".data(using: .utf8)!)
    exit(3)
}
// stop with the parent: when the effects process goes, stdin closes
Thread {
    while !FileHandle.standardInput.availableData.isEmpty {}
    exit(0)
}.start()
RunLoop.main.run()
