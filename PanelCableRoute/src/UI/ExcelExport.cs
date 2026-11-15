// -----------------------------------------------------------------------------
// Writing the table out: a CSV, and opening it in Excel when Excel is there.
//
// Excel is driven through late-bound COM by reflection, so the add-in needs no
// Interop assembly and no reference to a particular Office version. Every
// step is optional: if one is not allowed, the workbook is still open with the
// data in it, and if Excel cannot be started at all the CSV is already on disk.
// -----------------------------------------------------------------------------

using System;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows;

namespace PanelCableRoute.UI
{
    public static class ExcelExport
    {
        /// <summary>Where to write the file, or null when the user cancels.</summary>
        public static string AskForPath(Window owner)
        {
            Microsoft.Win32.SaveFileDialog dialog =
                new Microsoft.Win32.SaveFileDialog();
            dialog.Title = "Export panel cable routes";
            dialog.Filter = "Comma separated values (*.csv)|*.csv|"
                + "All files (*.*)|*.*";
            dialog.DefaultExt = ".csv";
            dialog.AddExtension = true;
            dialog.FileName = DefaultName();
            bool? answer = owner == null
                ? dialog.ShowDialog() : dialog.ShowDialog(owner);
            return answer == true ? dialog.FileName : null;
        }

        private static string DefaultName()
        {
            string stamp = DateTime.Now.ToString(
                "yyyy-MM-dd", CultureInfo.InvariantCulture);
            return "Panel cable routes " + stamp + ".csv";
        }

        /// <summary>
        /// UTF-8 with a byte-order mark, so Excel reads accented names
        /// correctly when the file is opened by double-clicking it.
        /// </summary>
        public static void WriteCsv(string path, string text)
        {
            File.WriteAllText(path, text, new UTF8Encoding(true));
        }

        /// <summary>
        /// Open the CSV in Excel, freeze the header, turn on filters and fit
        /// the columns. Returns null on success, or why it could not be done.
        /// savedAs is the .xlsx path when Excel agreed to save one.
        /// </summary>
        public static string OpenInExcel(string path, out string savedAs)
        {
            savedAs = null;
            object excel = null;
            try
            {
                Type excelType = Type.GetTypeFromProgID("Excel.Application");
                if (excelType == null)
                {
                    return "Excel is not installed on this machine";
                }
                excel = Activator.CreateInstance(excelType);
                Set(excel, "Visible", true);
                object workbooks = Get(excel, "Workbooks");
                object workbook = Call(workbooks, "Open", path);
                object sheet = Get(workbook, "ActiveSheet");

                Try(delegate
                {
                    object rows = Get(sheet, "Rows", 1);
                    object font = Get(rows, "Font");
                    Set(font, "Bold", true);
                });
                Try(delegate
                {
                    object used = Get(sheet, "UsedRange");
                    Call(used, "AutoFilter");
                });
                Try(delegate
                {
                    object columns = Get(sheet, "Columns");
                    Call(columns, "AutoFit");
                });
                Try(delegate
                {
                    object window = Get(excel, "ActiveWindow");
                    Set(window, "SplitRow", 1);
                    Set(window, "FreezePanes", true);
                });

                string target = path.EndsWith(
                        ".csv", StringComparison.OrdinalIgnoreCase)
                    ? path.Substring(0, path.Length - 4) + ".xlsx"
                    : path + ".xlsx";
                try
                {
                    // 51 = xlOpenXMLWorkbook (.xlsx)
                    Call(workbook, "SaveAs", target, 51);
                    savedAs = target;
                }
                catch (Exception)
                {
                    savedAs = null;     // the CSV is still open in Excel
                }
                return null;
            }
            catch (Exception error)
            {
                // Leave Excel alone if it did start: the user may still want
                // whatever did open.
                return Unwrap(error).Message;
            }
        }

        private static Exception Unwrap(Exception error)
        {
            TargetInvocationException wrapper =
                error as TargetInvocationException;
            if (wrapper != null && wrapper.InnerException != null)
            {
                return wrapper.InnerException;
            }
            return error;
        }

        private static void Try(Action step)
        {
            try
            {
                step();
            }
            catch (Exception)
            {
                // Formatting is a nicety; the data matters.
            }
        }

        private static object Get(object target, string name,
            params object[] arguments)
        {
            return target.GetType().InvokeMember(name,
                BindingFlags.GetProperty, null, target, arguments);
        }

        private static void Set(object target, string name, object value)
        {
            target.GetType().InvokeMember(name,
                BindingFlags.SetProperty, null, target, new object[] { value });
        }

        private static object Call(object target, string name,
            params object[] arguments)
        {
            return target.GetType().InvokeMember(name,
                BindingFlags.InvokeMethod, null, target, arguments);
        }
    }
}
