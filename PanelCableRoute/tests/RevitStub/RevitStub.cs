// -----------------------------------------------------------------------------
// A stand-in for the slice of the Revit API this add-in uses.
//
// WHY THIS EXISTS: the real RevitAPI.dll only ships with Revit, on Windows.
// Compiling src/Revit against this stub proves the C# is valid and internally
// consistent - names, types, casts, signatures as declared here - on any
// machine, which is far better than shipping code that has never seen a
// compiler.
//
// WHAT IT DOES NOT PROVE: that these signatures match the real Revit API. The
// members below were written from the API documentation, not generated from
// the assembly, so a wrong signature here would compile happily and fail in
// Revit. Building against the real RevitAPI.dll is still the only way to be
// sure, which is what src/PanelCableRoute.csproj does.
//
// It is NOT part of the add-in and must never be shipped with it.
// -----------------------------------------------------------------------------

using System;
using System.Collections;
using System.Collections.Generic;

namespace Autodesk.Revit.DB
{
    public class ElementId
    {
        public static readonly ElementId InvalidElementId = new ElementId(-1);
        private readonly long _value;
        public ElementId(long value) { _value = value; }
        public ElementId(int value) { _value = value; }
        public long Value { get { return _value; } }
        public int IntegerValue { get { return (int)_value; } }
        public override bool Equals(object other)
        {
            ElementId id = other as ElementId;
            return id != null && id._value == _value;
        }
        public override int GetHashCode() { return _value.GetHashCode(); }
        public static bool operator ==(ElementId a, ElementId b)
        {
            if (ReferenceEquals(a, null)) { return ReferenceEquals(b, null); }
            return a.Equals(b);
        }
        public static bool operator !=(ElementId a, ElementId b)
        {
            return !(a == b);
        }
    }

    public class XYZ
    {
        public XYZ(double x, double y, double z) { X = x; Y = y; Z = z; }
        public double X { get; private set; }
        public double Y { get; private set; }
        public double Z { get; private set; }
        public double DistanceTo(XYZ other) { return 0.0; }
        public double DotProduct(XYZ other) { return 0.0; }
        public XYZ CrossProduct(XYZ other) { return this; }
        public XYZ Normalize() { return this; }
        public XYZ Multiply(double factor) { return this; }
        public XYZ Subtract(XYZ other) { return this; }
        public double GetLength() { return 0.0; }
        public static XYZ operator -(XYZ a, XYZ b) { return a; }
        public static XYZ operator +(XYZ a, XYZ b) { return a; }
    }

    public class BoundingBoxXYZ
    {
        public XYZ Min { get; set; }
        public XYZ Max { get; set; }
    }

    public class Color
    {
        public Color(byte red, byte green, byte blue) { }
    }

    public enum BuiltInCategory
    {
        OST_CableTray = -2008130,
        OST_CableTrayFitting = -2008131,
        OST_ElectricalEquipment = -2001040,
        OST_ElectricalFixtures = -2001060,
        OST_GenericModel = -2000151,
        OST_SpecialityEquipment = -2001350,
        OST_MechanicalEquipment = -2001140,
        OST_Lines = -2000051
    }

    public enum BuiltInParameter
    {
        ALL_MODEL_INSTANCE_COMMENTS,
        ALL_MODEL_TYPE_COMMENTS,
        FAMILY_LEVEL_PARAM,
        RBS_CABLETRAY_WIDTH_PARAM,
        RBS_CABLETRAY_HEIGHT_PARAM
    }

    public enum GraphicsStyleType { Projection, Cut }

    public enum ViewType { FloorPlan, CeilingPlan, EngineeringPlan, ThreeD }

    public enum TransactionStatus { Uninitialized, Started, Committed, RolledBack }

    public class Parameter
    {
        public string AsString() { return null; }
        public string AsValueString() { return null; }
        public double AsDouble() { return 0.0; }
        public ElementId AsElementId() { return ElementId.InvalidElementId; }
    }

    public class Location { }

    public class LocationPoint : Location
    {
        public XYZ Point { get; set; }
    }

    public class LocationCurve : Location
    {
        public Curve Curve { get; set; }
    }

    public class Curve
    {
        public XYZ GetEndPoint(int index) { return null; }
        public double Length { get { return 0.0; } }
    }

    public class Line : Curve
    {
        public static Line CreateBound(XYZ start, XYZ end) { return null; }
    }

    public class GraphicsStyle : Element { }

    public class Category
    {
        public string Name { get; set; }
        public ElementId Id { get; set; }
        public Color LineColor { get; set; }
        public CategoryNameMap SubCategories { get; set; }
        public GraphicsStyle GetGraphicsStyle(GraphicsStyleType type)
        {
            return null;
        }
        public void SetLineWeight(int weight, GraphicsStyleType type) { }
        public static Category GetCategory(Document doc, BuiltInCategory which)
        {
            return null;
        }
    }

    public class CategoryNameMap : IEnumerable
    {
        public IEnumerator GetEnumerator() { yield break; }
    }

    public class Categories
    {
        public Category NewSubcategory(Category parent, string name)
        {
            return null;
        }
    }

    public class Settings
    {
        public Categories Categories { get; set; }
    }

    public class Element
    {
        public ElementId Id { get; set; }
        public string Name { get; set; }
        public Category Category { get; set; }
        public Location Location { get; set; }
        public ElementId LevelId { get; set; }
        public ElementId GetTypeId() { return ElementId.InvalidElementId; }
        public Parameter get_Parameter(BuiltInParameter which) { return null; }
        public Parameter LookupParameter(string name) { return null; }
        public BoundingBoxXYZ get_BoundingBox(View view) { return null; }
    }

    public class ElementSet : IEnumerable
    {
        public IEnumerator GetEnumerator() { yield break; }
    }

    public class Level : Element { }

    public class Family : Element { }

    public class FamilySymbol : Element
    {
        public Family Family { get; set; }
    }

    public class Connector
    {
        public XYZ Origin { get; set; }
        public ConnectorSet AllRefs { get; set; }
        public Element Owner { get; set; }
    }

    public class ConnectorSet : IEnumerable
    {
        public IEnumerator GetEnumerator() { yield break; }
    }

    public class ConnectorManager
    {
        public ConnectorSet Connectors { get; set; }
    }

    public class MEPModel
    {
        public ConnectorManager ConnectorManager { get; set; }
    }

    public class FamilyInstance : Element
    {
        public MEPModel MEPModel { get; set; }
    }

    public class MEPCurve : Element
    {
        public ConnectorManager ConnectorManager { get; set; }
    }

    public class View : Element
    {
        public ViewType ViewType { get; set; }
        public XYZ Origin { get; set; }
        public XYZ ViewDirection { get; set; }
        public bool IsTemplate { get; set; }
    }

    public class ViewPlan : View
    {
        public Level GenLevel { get; set; }
    }

    public class CurveElement : Element
    {
        public bool ViewSpecific { get; set; }
        public ElementId OwnerViewId { get; set; }
        public GraphicsStyle LineStyle { get; set; }
    }

    public class DetailCurve : CurveElement { }

    public class ItemFactoryBase
    {
        public DetailCurve NewDetailCurve(View view, Curve curve)
        {
            return null;
        }
    }

    public class FilteredElementCollector : IEnumerable<Element>
    {
        public FilteredElementCollector(Document doc) { }
        public FilteredElementCollector(Document doc, ElementId viewId) { }
        public FilteredElementCollector OfCategory(BuiltInCategory which)
        {
            return this;
        }
        public FilteredElementCollector OfClass(Type type) { return this; }
        public FilteredElementCollector WhereElementIsNotElementType()
        {
            return this;
        }
        public IList<Element> ToElements() { return new List<Element>(); }
        public int GetElementCount() { return 0; }
        public IEnumerator<Element> GetEnumerator()
        {
            yield break;
        }
        IEnumerator IEnumerable.GetEnumerator() { return GetEnumerator(); }
    }

    public class Transaction : IDisposable
    {
        public Transaction(Document doc, string name) { }
        public TransactionStatus Start() { return TransactionStatus.Started; }
        public TransactionStatus Commit() { return TransactionStatus.Committed; }
        public TransactionStatus RollBack() { return TransactionStatus.RolledBack; }
        public TransactionStatus GetStatus() { return TransactionStatus.Started; }
        public void Dispose() { }
    }

    public class ForgeTypeId
    {
        public ForgeTypeId(string id) { }
    }

    public static class UnitTypeId
    {
        public static ForgeTypeId Amperes = new ForgeTypeId("amperes");
        public static ForgeTypeId Volts = new ForgeTypeId("volts");
    }

    public static class UnitUtils
    {
        public static double ConvertFromInternalUnits(
            double value, ForgeTypeId unit)
        {
            return value;
        }
    }

    public class Document
    {
        public Settings Settings { get; set; }
        public ItemFactoryBase Create { get; set; }
        public ApplicationServices.Application Application { get; set; }
        public View ActiveView { get; set; }
        public Element GetElement(ElementId id) { return null; }
        public void Delete(ElementId id) { }
    }
}

namespace Autodesk.Revit.DB.Electrical
{
    public class ElectricalSystem : Element
    {
        public Element BaseEquipment { get; set; }
        public ElementSet Elements { get; set; }
        public string CircuitNumber { get; set; }
        public string LoadName { get; set; }
        public double Current { get; set; }
        public double Voltage { get; set; }
    }
}

namespace Autodesk.Revit.ApplicationServices
{
    public class Application
    {
        public double ShortCurveTolerance { get { return 0.00256; } }
    }
}
