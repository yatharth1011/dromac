plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Dromac's Bluetooth helper. Targets Android 11 (API 30) on purpose: since
// Android 13 an app targeting 33+ can no longer switch Bluetooth on itself,
// but one targeting 30 still can. Dromac (same signing key) asks it to when
// the Mac has lost the phone's audio and the phone has to take the
// headphones back -- with its Bluetooth still off from the Mac having them.
android {
    namespace = "com.dromac.bthelper"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.dromac.bthelper"
        minSdk = 26
        //noinspection ExpiredTargetSdkVersion
        targetSdk = 30
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")  // same key as Dromac
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
