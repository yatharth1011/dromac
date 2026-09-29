package com.dromac.app

import android.app.admin.DeviceAdminReceiver

// Only requests the "force-lock" policy (see res/xml/device_admin.xml) --
// enough to let the Mac dashboard remotely lock the phone, nothing more.
class DromacDeviceAdminReceiver : DeviceAdminReceiver()
