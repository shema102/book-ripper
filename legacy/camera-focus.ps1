param(
    [string]$CameraName = 'HD Pro Webcam C920',
    [int]$Focus = -1,
    [switch]$AutoFocus
)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

[ComImport, Guid("29840822-5B84-11D0-BD3B-00A0C911CE86"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IScanCreateDevEnum {
    [PreserveSig] int CreateClassEnumerator([In] ref Guid category, out IEnumMoniker enumerator, int flags);
}
[ComImport, Guid("55272A00-42CB-11CE-8135-00AA004BB851"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IScanPropertyBag {
    [PreserveSig] int Read([MarshalAs(UnmanagedType.LPWStr)] string name,
        [MarshalAs(UnmanagedType.Struct)] out object value, IntPtr errorLog);
    [PreserveSig] int Write([MarshalAs(UnmanagedType.LPWStr)] string name,
        [MarshalAs(UnmanagedType.Struct)] ref object value);
}
[ComImport, Guid("C6E13370-30AC-11D0-A18C-00A0C9118956"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IScanCameraControl {
    [PreserveSig] int GetRange(int property, out int min, out int max, out int step, out int defaultValue, out int caps);
    [PreserveSig] int Set(int property, int value, int flags);
    [PreserveSig] int Get(int property, out int value, out int flags);
}
public class ScanFocusResult {
    public string Camera;
    public int Value, Flags, Min, Max, Step, DefaultValue, Capabilities;
}
public static class ScanCameraFocus {
    public static ScanFocusResult Execute(string name, int requested, bool autoFocus) {
        Guid category = new Guid("860BB310-5D01-11D0-BD3B-00A0C911CE86");
        object dev = Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")));
        IEnumMoniker enumerator = null;
        try {
            int hr = ((IScanCreateDevEnum)dev).CreateClassEnumerator(ref category, out enumerator, 0);
            if (hr != 0 || enumerator == null) throw new Exception("No webcam devices found.");
            IMoniker[] monikers = new IMoniker[1];
            while (enumerator.Next(1, monikers, IntPtr.Zero) == 0) {
                object bagObject = null, cameraObject = null;
                try {
                    Guid bagId = typeof(IScanPropertyBag).GUID;
                    monikers[0].BindToStorage(null, null, ref bagId, out bagObject);
                    object friendly;
                    Marshal.ThrowExceptionForHR(((IScanPropertyBag)bagObject).Read("FriendlyName", out friendly, IntPtr.Zero));
                    if (!String.Equals(Convert.ToString(friendly), name, StringComparison.OrdinalIgnoreCase)) continue;
                    Guid controlId = typeof(IScanCameraControl).GUID;
                    monikers[0].BindToObject(null, null, ref controlId, out cameraObject);
                    var control = (IScanCameraControl)cameraObject;
                    var result = new ScanFocusResult(); result.Camera = name;
                    Marshal.ThrowExceptionForHR(control.GetRange(6, out result.Min, out result.Max,
                        out result.Step, out result.DefaultValue, out result.Capabilities));
                    Marshal.ThrowExceptionForHR(control.Get(6, out result.Value, out result.Flags));
                    if (requested >= 0 || autoFocus) {
                        int value = requested >= 0 ? requested : result.Value;
                        if (value < result.Min || value > result.Max || (value-result.Min)%result.Step != 0)
                            throw new ArgumentException("Focus value is outside the camera range/step.");
                        // Some UVC devices retain a cached value while parking the lens on stream restart.
                        if (!autoFocus && result.Value == value && result.Flags == 2) {
                            int neighbor = value + result.Step <= result.Max ? value + result.Step : value - result.Step;
                            Marshal.ThrowExceptionForHR(control.Set(6, neighbor, 2));
                            System.Threading.Thread.Sleep(100);
                        }
                        Marshal.ThrowExceptionForHR(control.Set(6, value, autoFocus ? 1 : 2));
                        Marshal.ThrowExceptionForHR(control.Get(6, out result.Value, out result.Flags));
                    }
                    return result;
                } finally {
                    if (cameraObject != null) Marshal.ReleaseComObject(cameraObject);
                    if (bagObject != null) Marshal.ReleaseComObject(bagObject);
                    Marshal.ReleaseComObject(monikers[0]);
                }
            }
            throw new Exception("Requested webcam was not found.");
        } finally {
            if (enumerator != null) Marshal.ReleaseComObject(enumerator);
            Marshal.ReleaseComObject(dev);
        }
    }
}
'@
[ScanCameraFocus]::Execute($CameraName, $Focus, $AutoFocus.IsPresent) | ConvertTo-Json
