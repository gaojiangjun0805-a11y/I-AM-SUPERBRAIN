import UIKit
import WebKit

class GameViewController: UIViewController {

    private var webView: WKWebView!

    override func viewDidLoad() {
        super.viewDidLoad()
        setupWebView()
        loadGame()
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }
    override var supportedInterfaceOrientations: UIInterfaceOrientationMask { .portrait }

    // MARK: - Setup

    private func setupWebView() {
        let config = WKWebViewConfiguration()

        // Allow audio to play without user gesture (background music)
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []

        webView = WKWebView(frame: .zero, configuration: config)
        webView.translatesAutoresizingMaskIntoConstraints = false
        webView.isOpaque = false
        webView.backgroundColor = UIColor(red: 0.05, green: 0.04, blue: 0.1, alpha: 1)
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false

        // Disable zoom
        webView.scrollView.maximumZoomScale = 1.0
        webView.scrollView.minimumZoomScale = 1.0

        view.backgroundColor = UIColor(red: 0.05, green: 0.04, blue: 0.1, alpha: 1)
        view.addSubview(webView)

        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
    }

    private func loadGame() {
        guard let htmlURL = Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "Game") else {
            showError("游戏文件加载失败")
            return
        }
        // allowingReadAccessTo lets the WebView read local resources in the same folder
        webView.loadFileURL(htmlURL, allowingReadAccessTo: htmlURL.deletingLastPathComponent())
    }

    private func showError(_ msg: String) {
        let alert = UIAlertController(title: "错误", message: msg, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "确定", style: .default))
        present(alert, animated: true)
    }
}
