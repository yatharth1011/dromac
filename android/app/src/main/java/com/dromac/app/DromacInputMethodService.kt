package com.dromac.app

import android.inputmethodservice.InputMethodService
import android.view.KeyEvent
import android.view.View

// A real Input Method is the only non-root way to inject actual keystrokes
// into whatever field has focus -- AccessibilityService gestures/actions can
// tap and scroll, but can't type. This IME does nothing on its own; it just
// sits there (showing a small "typing from the Mac" strip instead of a normal
// keyboard) and relays whatever the Mac dashboard sends over HTTP into the
// currently focused field via the standard InputConnection APIs. The user
// switches to it as their keyboard only while actively remote-typing, then
// switches back to their normal one (Android's keyboard-switcher, not this
// app, handles that switch -- same as switching between Gboard and any
// other installed keyboard).
class DromacInputMethodService : InputMethodService() {

    companion object {
        @Volatile var instance: DromacInputMethodService? = null
    }

    override fun onCreate() {
        super.onCreate()
        instance = this
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    override fun onCreateInputView(): View {
        return layoutInflater.inflate(R.layout.keyboard_view, null)
    }

    fun commitText(text: String): Boolean {
        return try {
            currentInputConnection?.commitText(text, 1) == true
        } catch (_: Exception) {
            false
        }
    }

    fun backspace(): Boolean {
        return try {
            val ic = currentInputConnection ?: return false
            ic.deleteSurroundingText(1, 0)
            true
        } catch (_: Exception) {
            false
        }
    }

    fun enter(): Boolean {
        return try {
            val ic = currentInputConnection ?: return false
            ic.sendKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_ENTER))
            ic.sendKeyEvent(KeyEvent(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_ENTER))
            true
        } catch (_: Exception) {
            false
        }
    }
}
