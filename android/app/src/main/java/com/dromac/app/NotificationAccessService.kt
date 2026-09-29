package com.dromac.app

import android.service.notification.NotificationListenerService

class NotificationAccessService : NotificationListenerService() {

    companion object {
        @Volatile
        var instance: NotificationAccessService? = null
    }

    override fun onListenerConnected() {
        super.onListenerConnected()
        instance = this
    }

    override fun onListenerDisconnected() {
        super.onListenerDisconnected()
        if (instance == this) instance = null
    }

    override fun onDestroy() {
        super.onDestroy()
        if (instance == this) instance = null
    }
}
