using System.Collections.Generic;
using System.Linq;
using Autodesk.Revit.Attributes;
using Autodesk.Revit.DB;
using Autodesk.Revit.UI;

namespace MyRevitAddin.Commands
{
    /// <summary>
    /// Example of a command that MODIFIES the model: everything happens inside
    /// one Transaction, so a single Ctrl+Z undoes it.
    /// </summary>
    [Transaction(TransactionMode.Manual)]
    public class CountWallsCommand : IExternalCommand
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
            List<Wall> walls = new FilteredElementCollector(doc, doc.ActiveView.Id)
                .OfClass(typeof(Wall))
                .Cast<Wall>()
                .ToList();

            if (walls.Count == 0)
            {
                TaskDialog.Show("Count Walls", "No walls in the active view.");
                return Result.Cancelled;
            }

            using (var tx = new Transaction(doc, "Tag walls with count"))
            {
                tx.Start();
                for (int i = 0; i < walls.Count; i++)
                {
                    Parameter comments = walls[i].get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS);
                    if (comments != null && !comments.IsReadOnly)
                    {
                        comments.Set("Wall " + (i + 1) + " of " + walls.Count + " in " + doc.ActiveView.Name);
                    }
                }
                tx.Commit();
            }

            TaskDialog.Show("Count Walls", walls.Count + " walls updated. Ctrl+Z to undo.");
            return Result.Succeeded;
        }
    }
}
