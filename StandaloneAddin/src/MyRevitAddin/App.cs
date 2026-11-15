using System;
using System.Reflection;
using System.Windows.Media.Imaging;
using Autodesk.Revit.UI;

namespace MyRevitAddin
{
    /// <summary>
    /// Entry point. Revit calls OnStartup once when it launches and
    /// OnShutdown when it closes. Build the ribbon here.
    /// </summary>
    public class App : IExternalApplication
    {
        private const string TabName = "My Tools";

        public Result OnStartup(UIControlledApplication application)
        {
            try
            {
                application.CreateRibbonTab(TabName);
            }
            catch (Autodesk.Revit.Exceptions.ArgumentException)
            {
                // Tab already exists (another add-in of ours created it).
            }

            RibbonPanel panel = application.CreateRibbonPanel(TabName, "General");
            string assemblyPath = Assembly.GetExecutingAssembly().Location;

            var helloButton = new PushButtonData(
                "HelloCommand",
                "Hello\nRevit",
                assemblyPath,
                typeof(Commands.HelloCommand).FullName)
            {
                ToolTip = "Shows a summary of the current selection.",
                LargeImage = LoadIcon("icon32.png"),
                Image = LoadIcon("icon16.png"),
            };
            panel.AddItem(helloButton);

            var wallsButton = new PushButtonData(
                "CountWallsCommand",
                "Count\nWalls",
                assemblyPath,
                typeof(Commands.CountWallsCommand).FullName)
            {
                ToolTip = "Counts walls in the active view and writes the count into their Comments.",
                LargeImage = LoadIcon("icon32.png"),
                Image = LoadIcon("icon16.png"),
            };
            panel.AddItem(wallsButton);

            return Result.Succeeded;
        }

        public Result OnShutdown(UIControlledApplication application)
        {
            return Result.Succeeded;
        }

        /// <summary>Loads a PNG embedded as a WPF Resource; returns null if missing.</summary>
        private static BitmapImage LoadIcon(string fileName)
        {
            try
            {
                var uri = new Uri(
                    "pack://application:,,,/MyRevitAddin;component/Resources/" + fileName,
                    UriKind.Absolute);
                return new BitmapImage(uri);
            }
            catch
            {
                return null; // Button still works without an icon.
            }
        }
    }
}
