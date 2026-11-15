using System.Linq;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace MyRevitAddin.Commands
{
    /// <summary>Read-only command: no Transaction needed.</summary>
    [Transaction(TransactionMode.ReadOnly)]
    public class HelloCommand : IExternalCommand
    {
        public Result Execute(ExternalCommandData commandData, ref string message, ElementSet elements)
        {
            UIDocument uiDoc = commandData.Application.ActiveUIDocument;
            if (uiDoc == null)
            {
                message = "Open a project first.";
                return Result.Failed;
            }

            Document doc = uiDoc.Document;
            var selected = uiDoc.Selection.GetElementIds()
                .Select(id => doc.GetElement(id))
                .Where(e => e != null)
                .ToList();

            string summary = selected.Count == 0
                ? "Nothing is selected."
                : string.Join("\n", selected
                    .GroupBy(e => e.Category?.Name ?? "(no category)")
                    .Select(g => g.Key + ": " + g.Count()));

            TaskDialog.Show("Hello Revit",
                "Document: " + doc.Title + "\n" +
                "Revit: " + commandData.Application.Application.VersionNumber + "\n\n" +
                summary);

            return Result.Succeeded;
        }
    }
}
