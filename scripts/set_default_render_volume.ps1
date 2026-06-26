param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 100)]
    [int]$Volume
)

$code = @'
using System;
using System.Runtime.InteropServices;

public enum EDataFlow { eRender = 0, eCapture = 1, eAll = 2 }
public enum ERole { eConsole = 0, eMultimedia = 1, eCommunications = 2 }

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
public class MMDeviceEnumerator { }

[ComImport, Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDeviceEnumerator
{
    int EnumAudioEndpoints(EDataFlow dataFlow, int dwStateMask, IntPtr ppDevices);
    int GetDefaultAudioEndpoint(EDataFlow dataFlow, ERole role, out IMMDevice ppEndpoint);
}

[ComImport, Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IMMDevice
{
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams, out IAudioEndpointVolume ppInterface);
}

[ComImport, Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAudioEndpointVolume
{
    int RegisterControlChangeNotify(IntPtr pNotify);
    int UnregisterControlChangeNotify(IntPtr pNotify);
    int GetChannelCount(out uint pnChannelCount);
    int SetMasterVolumeLevel(float fLevelDB, Guid pguidEventContext);
    int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
    int GetMasterVolumeLevel(out float pfLevelDB);
    int GetMasterVolumeLevelScalar(out float pfLevel);
}

public class VolumeControl
{
    public static IAudioEndpointVolume GetDefaultVolume()
    {
        IMMDeviceEnumerator enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
        IMMDevice device;
        Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender, ERole.eMultimedia, out device));
        Guid iid = typeof(IAudioEndpointVolume).GUID;
        IAudioEndpointVolume volume;
        Marshal.ThrowExceptionForHR(device.Activate(ref iid, 23, IntPtr.Zero, out volume));
        return volume;
    }

    public static float Set(float value)
    {
        var volume = GetDefaultVolume();
        Marshal.ThrowExceptionForHR(volume.SetMasterVolumeLevelScalar(value, Guid.Empty));
        float current;
        Marshal.ThrowExceptionForHR(volume.GetMasterVolumeLevelScalar(out current));
        return current;
    }

    public static float Get()
    {
        var volume = GetDefaultVolume();
        float current;
        Marshal.ThrowExceptionForHR(volume.GetMasterVolumeLevelScalar(out current));
        return current;
    }
}
'@

Add-Type -TypeDefinition $code
$before = [VolumeControl]::Get()
$after = [VolumeControl]::Set($Volume / 100.0)
[pscustomobject]@{
    requested_volume = $Volume
    before_scalar = [math]::Round($before, 3)
    after_scalar = [math]::Round($after, 3)
    after_percent = [math]::Round($after * 100, 1)
} | ConvertTo-Json -Compress
