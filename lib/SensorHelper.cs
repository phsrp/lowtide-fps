// Tiny LibreHardwareMonitor bridge for fps_overlay.py.
// Prints one JSON line per interval with the sensors people actually quote:
//   CPU temp  -> "Core (Tctl/Tdie)" on AMD, "CPU Package" on Intel
//   CPU usage -> "CPU Total"
//   GPU temp  -> "GPU Core"
//   GPU usage -> "GPU Core" (load)
// Build: csc /r:LibreHardwareMonitorLib.dll SensorHelper.cs
using System;
using System.Globalization;
using System.Threading;
using LibreHardwareMonitor.Hardware;

class SensorHelper
{
    static string[] CpuTempNames = { "Core (Tctl/Tdie)", "CPU Package", "Core (Tctl)", "Core (Tdie)", "Core Max", "Core Average" };

    static ISensor Find(IHardware hw, SensorType type, string[] names)
    {
        foreach (string name in names)
        {
            foreach (ISensor s in hw.Sensors)
                if (s.SensorType == type && s.Name == name) return s;
            foreach (IHardware sub in hw.SubHardware)
                foreach (ISensor s in sub.Sensors)
                    if (s.SensorType == type && s.Name == name) return s;
        }
        return null;
    }

    static void Update(IHardware hw)
    {
        hw.Update();
        foreach (IHardware sub in hw.SubHardware) sub.Update();
    }

    static int GpuRank(HardwareType t)
    {
        if (t == HardwareType.GpuNvidia) return 0;
        if (t == HardwareType.GpuAmd) return 1;
        if (t == HardwareType.GpuIntel) return 2;
        return 99;
    }

    static string Num(ISensor s)
    {
        if (s == null || !s.Value.HasValue || float.IsNaN(s.Value.Value)) return "null";
        return s.Value.Value.ToString("0.0", CultureInfo.InvariantCulture);
    }

    static string Str(string v)
    {
        return "\"" + (v ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
    }

    static void Main(string[] args)
    {
        int interval = args.Length > 0 ? int.Parse(args[0]) : 1000;
        Computer pc = new Computer();
        pc.IsCpuEnabled = true;
        pc.IsGpuEnabled = true;
        pc.Open();

        IHardware cpu = null, gpu = null;
        foreach (IHardware hw in pc.Hardware)
        {
            if (hw.HardwareType == HardwareType.Cpu && cpu == null) cpu = hw;
            if (GpuRank(hw.HardwareType) < 99)
            {
                // skip GPUs without a core temp (e.g. the Ryzen iGPU) if a real one exists
                Update(hw);
                bool hasCore = Find(hw, SensorType.Temperature, new[] { "GPU Core" }) != null;
                if (gpu == null || (hasCore && GpuRank(hw.HardwareType) < GpuRank(gpu.HardwareType)))
                    gpu = hw;
            }
        }

        ISensor cpuTemp = null, cpuLoad = null, gpuTemp = null, gpuLoad = null;
        while (true)
        {
            if (cpu != null)
            {
                Update(cpu);
                if (cpuTemp == null) cpuTemp = Find(cpu, SensorType.Temperature, CpuTempNames);
                if (cpuLoad == null) cpuLoad = Find(cpu, SensorType.Load, new[] { "CPU Total" });
            }
            if (gpu != null)
            {
                Update(gpu);
                if (gpuTemp == null) gpuTemp = Find(gpu, SensorType.Temperature, new[] { "GPU Core", "GPU Hot Spot" });
                if (gpuLoad == null) gpuLoad = Find(gpu, SensorType.Load, new[] { "GPU Core", "D3D 3D" });
            }
            Console.WriteLine("{" +
                "\"cpu_name\":" + Str(cpu != null ? cpu.Name : null) +
                ",\"cpu_temp\":" + Num(cpuTemp) +
                ",\"cpu_temp_sensor\":" + Str(cpuTemp != null ? cpuTemp.Name : null) +
                ",\"cpu_load\":" + Num(cpuLoad) +
                ",\"gpu_name\":" + Str(gpu != null ? gpu.Name : null) +
                ",\"gpu_temp\":" + Num(gpuTemp) +
                ",\"gpu_temp_sensor\":" + Str(gpuTemp != null ? gpuTemp.Name : null) +
                ",\"gpu_load\":" + Num(gpuLoad) + "}");
            Console.Out.Flush();
            Thread.Sleep(interval);
        }
    }
}
