package com.zeroxai.app

import android.annotation.SuppressLint
import android.graphics.Bitmap
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var progressBar: ProgressBar
    private lateinit var errorView: FrameLayout
    private lateinit var errorMessage: TextView
    private var isLoading = false

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        progressBar = findViewById(R.id.progressBar)
        webView = findViewById(R.id.webView)
        errorView = findViewById(R.id.errorView)
        errorMessage = findViewById(R.id.errorMessage)
        val retryButton = findViewById<Button>(R.id.retryButton)

        retryButton.setOnClickListener {
            hideError()
            webView.reload()
        }

        webView.apply {
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                loadWithOverviewMode = true
                useWideViewPort = true
                builtInZoomControls = false
                displayZoomControls = false
                setSupportZoom(false)
                userAgentString = settings.userAgentString
                    .replace("; wv", "")
                    .replace("Version/\\d+\\.\\d+", "")

                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    safeBrowsingEnabled = true
                }
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
                }
            }

            webViewClient = object : WebViewClient() {
                override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                    isLoading = true
                    progressBar.visibility = View.VISIBLE
                }

                override fun onPageFinished(view: WebView?, url: String?) {
                    isLoading = false
                    progressBar.visibility = View.GONE
                }

                override fun shouldOverrideUrlLoading(
                    view: WebView?,
                    request: WebResourceRequest?
                ): Boolean {
                    return false
                }

                override fun onReceivedError(
                    view: WebView?,
                    request: WebResourceRequest?,
                    error: WebResourceError?
                ) {
                    if (request?.isForMainFrame == true) {
                        showError(
                            error?.description?.toString()
                                ?: "Неизвестная ошибка"
                        )
                    }
                }

                override fun onReceivedSslError(
                    view: WebView?,
                    handler: SslErrorHandler?,
                    error: SslError?
                ) {
                    handler?.cancel()
                    showError("Ошибка SSL: ${error?.getCertificatePath()?.toString() ?: "неизвестно"}")
                }
            }

            webChromeClient = object : WebChromeClient() {
                override fun onProgressChanged(view: WebView?, newProgress: Int) {
                    progressBar.progress = newProgress
                }
            }

            if (WebViewFeature.isFeatureSupported(WebViewFeature.FORCE_DARK)) {
                WebViewCompat.setForceDark(this, WebViewCompat.FORCE_DARK_AUTO)
            }

            loadUrl("https://artistic-happiness-production.up.railway.app")
        }
    }

    private fun showError(message: String) {
        progressBar.visibility = View.GONE
        errorMessage.text = message
        errorView.visibility = View.VISIBLE
        webView.visibility = View.GONE
    }

    private fun hideError() {
        errorView.visibility = View.GONE
        webView.visibility = View.VISIBLE
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        webView.saveState(outState)
    }

    override fun onRestoreInstanceState(savedInstanceState: Bundle) {
        super.onRestoreInstanceState(savedInstanceState)
        webView.restoreState(savedInstanceState)
    }
}
