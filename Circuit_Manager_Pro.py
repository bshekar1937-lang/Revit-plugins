# -*- coding: utf-8 -*-
__title__ = "Circuit Manager Pro"
__doc__ = (
    "Give many fixtures their own circuit each, move or disconnect or delete "
    "circuits in bulk, and add or remove elements on a circuit - all from one "
    "dialog. Every action is a separate Undo step."
)

# =============================================================================
# CIRCUIT MANAGER PRO  -  pyRevit script for Revit electrical circuits
# -----------------------------------------------------------------------------
# Opens one dialog with three tabs:
#
#   1. CREATE   Pick many fixtures / devices and give EACH ONE its own power
#               circuit (one circuit per element), optionally assigned straight
#               to a panel. Elements already on a power circuit can be skipped.
#   2. MANAGE   List every circuit (filter by panel / unassigned / text).
#               Multi-select circuits and in bulk:
#                 - move them to another panel
#                 - disconnect them from their panel
#                 - delete them
#               Circuits can also be selected by picking elements in the model.
#   3. EDIT     Choose one circuit, see the elements on it, then
#                 - remove selected elements from the circuit
#                 - add more elements to the circuit (pick in the model)
#
# Every action runs in its own Revit transaction, so each one can be undone
# separately with Ctrl+Z after the dialog is closed.
#
# HOW TO USE
#   pyRevit: put this file in a pushbutton folder as script.py, e.g.
#       MyExtension.extension/Electrical.tab/Circuits.panel/
#           Circuit Manager Pro.pushbutton/script.py
#   then pyRevit > Reload. Works with the default IronPython engine.
#   RevitPythonShell: open the file and run it as-is.
#   When the dialog closes, the action log is printed to the output window.
#
# Revit 2022 - 2027. Element ids are read with eid_val() because
# ElementId.IntegerValue was removed in Revit 2026.
# =============================================================================

import clr

clr.AddReference("RevitAPI")
clr.AddReference("RevitAPIUI")
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xml")

import System
from System.Collections.Generic import List

from Autodesk.Revit import DB
from Autodesk.Revit.DB.Electrical import ElectricalSystem, ElectricalSystemType
from Autodesk.Revit.UI.Selection import ObjectType

from System.Windows.Controls import ComboBoxItem, ListBoxItem
from System.Windows.Markup import XamlReader

uidoc = __revit__.ActiveUIDocument   # provided by pyRevit / RevitPythonShell
doc = uidoc.Document


# =============================================================================
# Revit helpers
# =============================================================================
def eid_val(eid):
    """Integer value of an ElementId on every Revit version."""
    try:
        return int(eid.Value)          # Revit 2024+
    except Exception:
        return int(eid.IntegerValue)   # Revit 2023 and older


def to_eid(value):
    try:
        return DB.ElementId(System.Int64(value))
    except Exception:
        return DB.ElementId(int(value))


def get_el(value):
    return doc.GetElement(to_eid(value))


# Categories that can be put on a power circuit
CIRCUITABLE_BICS = [
    DB.BuiltInCategory.OST_LightingFixtures,
    DB.BuiltInCategory.OST_LightingDevices,
    DB.BuiltInCategory.OST_ElectricalFixtures,
    DB.BuiltInCategory.OST_ElectricalEquipment,
    DB.BuiltInCategory.OST_MechanicalEquipment,
    DB.BuiltInCategory.OST_SpecialityEquipment,
    DB.BuiltInCategory.OST_DataDevices,
    DB.BuiltInCategory.OST_CommunicationDevices,
    DB.BuiltInCategory.OST_FireAlarmDevices,
    DB.BuiltInCategory.OST_SecurityDevices,
    DB.BuiltInCategory.OST_NurseCallDevices,
    DB.BuiltInCategory.OST_TelephoneDevices,
]
CIRCUITABLE_CAT_IDS = set()
for _bic in CIRCUITABLE_BICS:
    try:
        CIRCUITABLE_CAT_IDS.add(eid_val(DB.ElementId(_bic)))
    except Exception:
        pass


def has_electrical_connector(el):
    try:
        mep = el.MEPModel
        if mep is None or mep.ConnectorManager is None:
            return False
        for c in mep.ConnectorManager.Connectors:
            if c.Domain == DB.Domain.DomainElectrical:
                return True
    except Exception:
        pass
    return False


def is_circuitable(el):
    if not isinstance(el, DB.FamilyInstance) or el.Category is None:
        return False
    if eid_val(el.Category.Id) not in CIRCUITABLE_CAT_IDS:
        return False
    return has_electrical_connector(el)


def element_systems(el):
    """Electrical systems (circuits) an element belongs to."""
    out = []
    try:
        mep = el.MEPModel
        try:
            systems = mep.GetElectricalSystems()       # Revit 2021+
        except Exception:
            systems = mep.ElectricalSystems             # older API
        if systems:
            for s in systems:
                out.append(s)
    except Exception:
        pass
    return out


def power_systems(el):
    return [s for s in element_systems(el)
            if s.SystemType == ElectricalSystemType.PowerCircuit]


def element_label(el):
    try:
        cat = el.Category.Name if el.Category else "?"
        fam = el.Symbol.FamilyName if isinstance(el, DB.FamilyInstance) else ""
        typ = el.Name
        mark = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_MARK)
        mark = mark.AsString() if mark and mark.AsString() else ""
        txt = u"{0}  :  {1} - {2}".format(cat, fam, typ)
        if mark:
            txt += u"   [Mark {0}]".format(mark)
        return txt + u"   (id {0})".format(eid_val(el.Id))
    except Exception:
        return u"Element {0}".format(eid_val(el.Id))


def panel_name(panel):
    try:
        p = panel.get_Parameter(DB.BuiltInParameter.RBS_ELEC_PANEL_NAME)
        if p and p.AsString():
            return p.AsString()
    except Exception:
        pass
    return panel.Name


def all_panels():
    panels = []
    col = (DB.FilteredElementCollector(doc)
           .OfCategory(DB.BuiltInCategory.OST_ElectricalEquipment)
           .WhereElementIsNotElementType())
    for e in col:
        if isinstance(e, DB.FamilyInstance) and e.MEPModel is not None:
            panels.append(e)
    panels.sort(key=lambda p: panel_name(p).lower())
    return panels


def all_circuits():
    col = DB.FilteredElementCollector(doc).OfClass(ElectricalSystem)
    circuits = [c for c in col]

    def key(c):
        pn = (c.PanelName or u"~unassigned").lower()
        num = c.CircuitNumber or u""
        try:
            n = int(num.split(",")[0])
        except Exception:
            n = 99999
        return (pn, n, num)
    circuits.sort(key=key)
    return circuits


def circuit_label(c):
    try:
        n = c.Elements.Size
    except Exception:
        n = 0
    try:
        cls = c.LoadClassifications or u"-"
    except Exception:
        cls = u"-"
    try:
        stype = str(c.SystemType).replace("Circuit", "")
    except Exception:
        stype = u""
    panel = c.PanelName if c.PanelName else u"<no panel>"
    num = c.CircuitNumber if c.CircuitNumber else u"-"
    load = c.LoadName if c.LoadName else u""
    return u"{0}   |   Ckt {1}   |   {2}   |   {3} elem   |   {4}   |   {5}".format(
        panel, num, load, n, cls, stype)


def pick_elements(prompt):
    """Let the user pick elements; returns [] on Esc."""
    try:
        refs = uidoc.Selection.PickObjects(ObjectType.Element, prompt)
    except Exception:
        return []
    return [doc.GetElement(r.ElementId) for r in refs]


def make_set(elements):
    es = DB.ElementSet()
    for e in elements:
        es.Insert(e)
    return es


class Tx(object):
    """with Tx("name"): ...  -> one undoable Revit transaction."""

    def __init__(self, name):
        self.t = DB.Transaction(doc, "Circuit Manager - " + name)

    def __enter__(self):
        self.t.Start()
        opts = self.t.GetFailureHandlingOptions()
        opts.SetClearAfterRollback(True)
        self.t.SetFailureHandlingOptions(opts)
        return self.t

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.t.Commit()
        else:
            self.t.RollBack()
        return False


# =============================================================================
# Dialog (WPF / XAML) - raised "3D" cards, glossy buttons, spinning 3D cube
# =============================================================================
XAML = u"""
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Circuit Manager Pro" Width="1040" Height="720"
        MinWidth="860" MinHeight="600"
        WindowStartupLocation="CenterScreen" FontFamily="Segoe UI" FontSize="13"
        Background="#E9EEF5">
  <Window.Resources>
    <DropShadowEffect x:Key="CardShadow" BlurRadius="18" ShadowDepth="5"
                      Direction="270" Opacity="0.28" Color="#1B2A41"/>
    <DropShadowEffect x:Key="BtnShadow" BlurRadius="8" ShadowDepth="3"
                      Direction="270" Opacity="0.40" Color="#0B1626"/>

    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background">
        <Setter.Value>
          <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
            <GradientStop Color="#FFFFFF" Offset="0"/>
            <GradientStop Color="#F3F6FB" Offset="1"/>
          </LinearGradientBrush>
        </Setter.Value>
      </Setter>
      <Setter Property="BorderBrush" Value="#D3DCE8"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="12"/>
      <Setter Property="Padding" Value="14"/>
      <Setter Property="Margin" Value="8"/>
      <Setter Property="Effect" Value="{StaticResource CardShadow}"/>
    </Style>

    <Style x:Key="Btn3D" TargetType="Button">
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="4"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root" RenderTransformOrigin="0.5,0.5">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border x:Name="base" CornerRadius="9" Margin="0,3,0,0"
                      Background="#16325F"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#6FA2FF" Offset="0"/>
                    <GradientStop Color="#2F6FE4" Offset="0.55"/>
                    <GradientStop Color="#2457B8" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <Grid>
                  <Border CornerRadius="9,9,30,30" Height="12"
                          VerticalAlignment="Top" Margin="3,2,3,0">
                    <Border.Background>
                      <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                        <GradientStop Color="#80FFFFFF" Offset="0"/>
                        <GradientStop Color="#00FFFFFF" Offset="1"/>
                      </LinearGradientBrush>
                    </Border.Background>
                  </Border>
                  <ContentPresenter Margin="{TemplateBinding Padding}"
                                    HorizontalAlignment="Center"
                                    VerticalAlignment="Center"/>
                </Grid>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Opacity" Value="0.95"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnDanger" TargetType="Button" BasedOn="{StaticResource Btn3D}">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border CornerRadius="9" Margin="0,3,0,0" Background="#6B1414"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#FF8A80" Offset="0"/>
                    <GradientStop Color="#E0453A" Offset="0.55"/>
                    <GradientStop Color="#B3261E" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <ContentPresenter Margin="{TemplateBinding Padding}"
                                  HorizontalAlignment="Center"
                                  VerticalAlignment="Center"/>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="BtnGreen" TargetType="Button" BasedOn="{StaticResource Btn3D}">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Grid x:Name="root">
              <Grid.RenderTransform>
                <TranslateTransform Y="0"/>
              </Grid.RenderTransform>
              <Border CornerRadius="9" Margin="0,3,0,0" Background="#0F4D2C"/>
              <Border x:Name="face" CornerRadius="9" Margin="0,0,0,3"
                      Effect="{StaticResource BtnShadow}">
                <Border.Background>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                    <GradientStop Color="#7EE2A8" Offset="0"/>
                    <GradientStop Color="#22A55B" Offset="0.55"/>
                    <GradientStop Color="#17803F" Offset="1"/>
                  </LinearGradientBrush>
                </Border.Background>
                <ContentPresenter Margin="{TemplateBinding Padding}"
                                  HorizontalAlignment="Center"
                                  VerticalAlignment="Center"/>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="-2"/></Setter.Value></Setter>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="root" Property="RenderTransform"><Setter.Value><TranslateTransform Y="2"/></Setter.Value></Setter>
                <Setter TargetName="face" Property="Margin" Value="0,2,0,1"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="root" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <Style x:Key="H2" TargetType="TextBlock">
      <Setter Property="FontSize" Value="15"/>
      <Setter Property="FontWeight" Value="Bold"/>
      <Setter Property="Foreground" Value="#1B2A41"/>
      <Setter Property="Margin" Value="0,0,0,8"/>
    </Style>
    <Style x:Key="Hint" TargetType="TextBlock">
      <Setter Property="Foreground" Value="#5A6B82"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Margin" Value="0,0,0,8"/>
    </Style>
    <Style TargetType="ListBox">
      <Setter Property="BorderBrush" Value="#C9D4E3"/>
      <Setter Property="Background" Value="#FBFCFE"/>
      <Setter Property="FontFamily" Value="Consolas, Segoe UI"/>
      <Setter Property="FontSize" Value="12.5"/>
    </Style>
    <Style TargetType="ComboBox">
      <Setter Property="Padding" Value="6,4"/>
      <Setter Property="Margin" Value="4"/>
      <Setter Property="MinWidth" Value="200"/>
    </Style>
    <Style TargetType="TabItem">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TabItem">
            <Border x:Name="b" CornerRadius="10,10,0,0" Margin="2,0"
                    Padding="18,8" Background="#C8D3E3">
              <ContentPresenter ContentSource="Header"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsSelected" Value="True">
                <Setter TargetName="b" Property="Background">
                  <Setter.Value>
                    <LinearGradientBrush StartPoint="0,0" EndPoint="0,1">
                      <GradientStop Color="#FFFFFF" Offset="0"/>
                      <GradientStop Color="#F3F6FB" Offset="1"/>
                    </LinearGradientBrush>
                  </Setter.Value>
                </Setter>
                <Setter Property="FontWeight" Value="Bold"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="120"/>
    </Grid.RowDefinitions>

    <!-- ===== Header with spinning 3D cube ===== -->
    <Border Grid.Row="0" Margin="8,8,8,4" CornerRadius="14" Padding="14,8"
            Effect="{StaticResource CardShadow}">
      <Border.Background>
        <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
          <GradientStop Color="#0F2447" Offset="0"/>
          <GradientStop Color="#1E4FA3" Offset="0.6"/>
          <GradientStop Color="#2F7BEA" Offset="1"/>
        </LinearGradientBrush>
      </Border.Background>
      <DockPanel>
        <Viewport3D DockPanel.Dock="Left" Width="70" Height="64" ClipToBounds="False">
          <Viewport3D.Camera>
            <PerspectiveCamera Position="3.2,2.6,4.2" LookDirection="-3.2,-2.6,-4.2"
                               UpDirection="0,1,0" FieldOfView="45"/>
          </Viewport3D.Camera>
          <ModelVisual3D>
            <ModelVisual3D.Content>
              <Model3DGroup>
                <AmbientLight Color="#555555"/>
                <DirectionalLight Color="#FFFFFF" Direction="-1,-1.5,-2"/>
                <GeometryModel3D>
                  <GeometryModel3D.Geometry>
                    <MeshGeometry3D
                      Positions="-1,-1,1 1,-1,1 1,1,1 -1,1,1  1,-1,-1 -1,-1,-1 -1,1,-1 1,1,-1  -1,-1,-1 -1,-1,1 -1,1,1 -1,1,-1  1,-1,1 1,-1,-1 1,1,-1 1,1,1  -1,1,1 1,1,1 1,1,-1 -1,1,-1  -1,-1,-1 1,-1,-1 1,-1,1 -1,-1,1"
                      TriangleIndices="0 1 2 0 2 3  4 5 6 4 6 7  8 9 10 8 10 11  12 13 14 12 14 15  16 17 18 16 18 19  20 21 22 20 22 23"/>
                  </GeometryModel3D.Geometry>
                  <GeometryModel3D.Material>
                    <DiffuseMaterial>
                      <DiffuseMaterial.Brush>
                        <LinearGradientBrush StartPoint="0,0" EndPoint="1,1">
                          <GradientStop Color="#FFD54F" Offset="0"/>
                          <GradientStop Color="#FF8F00" Offset="1"/>
                        </LinearGradientBrush>
                      </DiffuseMaterial.Brush>
                    </DiffuseMaterial>
                  </GeometryModel3D.Material>
                  <GeometryModel3D.Transform>
                    <RotateTransform3D>
                      <RotateTransform3D.Rotation>
                        <AxisAngleRotation3D x:Name="cubeRot" Axis="0.3,1,0.2" Angle="0"/>
                      </RotateTransform3D.Rotation>
                    </RotateTransform3D>
                  </GeometryModel3D.Transform>
                </GeometryModel3D>
              </Model3DGroup>
            </ModelVisual3D.Content>
          </ModelVisual3D>
          <Viewport3D.Triggers>
            <EventTrigger RoutedEvent="Viewport3D.Loaded">
              <BeginStoryboard>
                <Storyboard>
                  <DoubleAnimation Storyboard.TargetName="cubeRot"
                                   Storyboard.TargetProperty="Angle"
                                   From="0" To="360" Duration="0:0:8"
                                   RepeatBehavior="Forever"/>
                </Storyboard>
              </BeginStoryboard>
            </EventTrigger>
          </Viewport3D.Triggers>
        </Viewport3D>
        <StackPanel VerticalAlignment="Center" Margin="12,0,0,0">
          <TextBlock Text="&#x26A1; CIRCUIT MANAGER PRO" Foreground="White"
                     FontSize="22" FontWeight="Bold">
            <TextBlock.Effect>
              <DropShadowEffect BlurRadius="6" ShadowDepth="2" Opacity="0.6"/>
            </TextBlock.Effect>
          </TextBlock>
          <TextBlock x:Name="txtDoc" Foreground="#CFE0FF" FontSize="12"
                     Text="Create, move, edit and remove electrical circuits in bulk"/>
        </StackPanel>
      </DockPanel>
    </Border>

    <!-- ===== Tabs ===== -->
    <TabControl x:Name="tabs" Grid.Row="1" Margin="8,4,8,0" Background="Transparent"
                BorderThickness="0" Padding="0">

      <!-- ===== TAB 1 : CREATE ===== -->
      <TabItem Header="&#x2795;  Create circuits">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}"
                         x:Name="lblFixtures" Text="Fixtures to circuit (0)"/>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource Hint}"
                         Text="Each element in this list gets its OWN circuit. Pick in the model, then press Finish in Revit's options bar."/>
              <WrapPanel DockPanel.Dock="Bottom" Margin="0,8,0,0">
                <Button x:Name="btnPickFix" Style="{StaticResource Btn3D}" Content="&#x1F5B1; Pick fixtures..."/>
                <Button x:Name="btnRemFix" Style="{StaticResource Btn3D}" Content="Remove selected from list"/>
                <Button x:Name="btnClearFix" Style="{StaticResource BtnDanger}" Content="Clear list"/>
              </WrapPanel>
              <ListBox x:Name="lstFixtures" SelectionMode="Extended"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Options"/>
              <TextBlock Style="{StaticResource Hint}" Text="Assign the new circuits to panel:"/>
              <ComboBox x:Name="cmbCreatePanel"/>
              <CheckBox x:Name="chkSkip" Margin="4,12,4,4" IsChecked="True"
                        Content="Skip elements already on a power circuit"/>
              <TextBlock Style="{StaticResource Hint}" Margin="4,12,4,4"
                         Text="The panel's distribution system must match the voltage / poles of the fixtures, otherwise the circuit is created without a panel and the reason is logged below."/>
              <Button x:Name="btnCreate" Style="{StaticResource BtnGreen}" Margin="4,18,4,4"
                      Content="&#x26A1; Create one circuit per element" FontSize="14"/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>

      <!-- ===== TAB 2 : MANAGE ===== -->
      <TabItem Header="&#x21C4;  Move / remove circuits">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}"
                         x:Name="lblCircuits" Text="Circuits"/>
              <WrapPanel DockPanel.Dock="Top">
                <TextBlock Text="Panel:" VerticalAlignment="Center" Margin="0,0,4,0"/>
                <ComboBox x:Name="cmbFilterPanel"/>
                <TextBlock Text="Search:" VerticalAlignment="Center" Margin="10,0,4,0"/>
                <TextBox x:Name="txtSearch" Width="180" Padding="4" Margin="4"/>
              </WrapPanel>
              <WrapPanel DockPanel.Dock="Bottom" Margin="0,8,0,0">
                <Button x:Name="btnSelAll" Style="{StaticResource Btn3D}" Content="Select all"/>
                <Button x:Name="btnSelNone" Style="{StaticResource Btn3D}" Content="Select none"/>
                <Button x:Name="btnPickCkt" Style="{StaticResource Btn3D}" Content="&#x1F5B1; Select by picking elements..."/>
                <Button x:Name="btnRefresh" Style="{StaticResource Btn3D}" Content="&#x21BB; Refresh"/>
              </WrapPanel>
              <ListBox x:Name="lstCircuits" SelectionMode="Extended" Margin="0,6,0,0"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Bulk actions"/>
              <TextBlock Style="{StaticResource Hint}" x:Name="lblSelCount" Text="0 circuits selected"/>
              <TextBlock Style="{StaticResource Hint}" Text="Move selected circuits to panel:"/>
              <ComboBox x:Name="cmbMovePanel"/>
              <Button x:Name="btnMove" Style="{StaticResource BtnGreen}" Content="&#x21C4; Move to panel"/>
              <Separator Margin="0,14"/>
              <Button x:Name="btnDisconnect" Style="{StaticResource Btn3D}" Content="Disconnect from panel"/>
              <Button x:Name="btnEditSel" Style="{StaticResource Btn3D}" Content="Edit elements of selected circuit"/>
              <Separator Margin="0,14"/>
              <Button x:Name="btnDelete" Style="{StaticResource BtnDanger}" Content="&#x2716; Delete selected circuits"/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>

      <!-- ===== TAB 3 : EDIT ===== -->
      <TabItem Header="&#x270E;  Add / remove elements">
        <Grid>
          <Grid.ColumnDefinitions>
            <ColumnDefinition Width="*"/>
            <ColumnDefinition Width="320"/>
          </Grid.ColumnDefinitions>
          <Border Style="{StaticResource Card}">
            <DockPanel>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource H2}" Text="Circuit"/>
              <ComboBox DockPanel.Dock="Top" x:Name="cmbEditCircuit" Margin="0,0,0,8"/>
              <TextBlock DockPanel.Dock="Top" Style="{StaticResource Hint}"
                         x:Name="lblEditElems" Text="Elements on this circuit"/>
              <ListBox x:Name="lstEditElems" SelectionMode="Extended"/>
            </DockPanel>
          </Border>
          <Border Grid.Column="1" Style="{StaticResource Card}">
            <StackPanel>
              <TextBlock Style="{StaticResource H2}" Text="Actions"/>
              <Button x:Name="btnAddElems" Style="{StaticResource BtnGreen}" Content="&#x2795; Add elements (pick)..."/>
              <Button x:Name="btnRemElems" Style="{StaticResource BtnDanger}" Content="&#x2796; Remove selected elements"/>
              <TextBlock Style="{StaticResource Hint}" Margin="4,14,4,4"
                         Text="Revit will not remove the last element of a circuit - delete the circuit on the Move / remove tab instead."/>
            </StackPanel>
          </Border>
        </Grid>
      </TabItem>
    </TabControl>

    <!-- ===== Log ===== -->
    <Border Grid.Row="2" Style="{StaticResource Card}" Padding="8">
      <DockPanel>
        <Button DockPanel.Dock="Right" x:Name="btnClose" Style="{StaticResource Btn3D}"
                Content="Close" VerticalAlignment="Bottom" MinWidth="100"/>
        <TextBox x:Name="txtLog" IsReadOnly="True" BorderThickness="0" Background="Transparent"
                 FontFamily="Consolas" FontSize="12" Foreground="#24364F"
                 VerticalScrollBarVisibility="Auto" TextWrapping="Wrap"/>
      </DockPanel>
    </Border>
  </Grid>
</Window>
"""


# =============================================================================
# State kept between dialog openings (the dialog closes while you pick)
# =============================================================================
STATE = {
    "fixtures": [],           # element id ints for the Create tab
    "tab": 0,
    "log": [],
    "create_panel": None,     # panel id int or None
    "skip": True,
    "filter_panel": "__all__",
    "search": u"",
    "preselect": set(),       # circuit ids to select on the Manage tab
    "move_panel": None,
    "edit_circuit": None,
    "pending": None,          # "pick_fixtures" | "pick_circuits" | "add_elems"
}


def log(msg):
    STATE["log"].append(msg)


class CircuitManagerWindow(object):

    def __init__(self):
        self.win = XamlReader.Parse(XAML)
        f = self.win.FindName
        self.c = dict((n, f(n)) for n in [
            "tabs", "txtDoc", "lblFixtures", "lstFixtures", "btnPickFix", "btnRemFix",
            "btnClearFix", "cmbCreatePanel", "chkSkip", "btnCreate",
            "lblCircuits", "cmbFilterPanel", "txtSearch", "btnSelAll", "btnSelNone",
            "btnPickCkt", "btnRefresh", "lstCircuits", "lblSelCount", "cmbMovePanel",
            "btnMove", "btnDisconnect", "btnEditSel", "btnDelete",
            "cmbEditCircuit", "lblEditElems", "lstEditElems", "btnAddElems",
            "btnRemElems", "txtLog", "btnClose"])
        self.busy = False
        self.c["txtDoc"].Text = u"Project: {0}   -   each action is one Undo step".format(doc.Title)

        self.fill_panel_combos()
        self.c["chkSkip"].IsChecked = STATE["skip"]
        self.fill_fixtures()
        self.fill_circuits()
        self.fill_edit_combo()
        self.refresh_log()
        self.c["tabs"].SelectedIndex = STATE["tab"]

        ev = [
            ("btnPickFix", self.on_pick_fixtures), ("btnRemFix", self.on_remove_fix),
            ("btnClearFix", self.on_clear_fix), ("btnCreate", self.on_create),
            ("btnSelAll", self.on_sel_all), ("btnSelNone", self.on_sel_none),
            ("btnPickCkt", self.on_pick_circuits), ("btnRefresh", self.on_refresh),
            ("btnMove", self.on_move), ("btnDisconnect", self.on_disconnect),
            ("btnEditSel", self.on_edit_selected), ("btnDelete", self.on_delete),
            ("btnAddElems", self.on_add_elems), ("btnRemElems", self.on_remove_elems),
            ("btnClose", self.on_close),
        ]
        for name, handler in ev:
            self.c[name].Click += self.safe(handler)
        self.c["cmbFilterPanel"].SelectionChanged += self.safe(self.on_filter_changed)
        self.c["txtSearch"].TextChanged += self.safe(self.on_filter_changed)
        self.c["lstCircuits"].SelectionChanged += self.safe(self.on_circuit_sel_changed)
        self.c["cmbEditCircuit"].SelectionChanged += self.safe(self.on_edit_circuit_changed)

    def safe(self, handler):
        """Never let an error escape a WPF event (it could crash Revit)."""
        def run(sender, args):
            if self.busy:
                return
            self.busy = True
            try:
                handler(sender, args)
            except Exception as ex:
                log(u"Error: {0}".format(ex))
                try:
                    self.refresh_log()
                except Exception:
                    pass
            finally:
                self.busy = False
        return run

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def combo_item(text, tag):
        it = ComboBoxItem()
        it.Content = text
        it.Tag = tag
        return it

    @staticmethod
    def list_item(text, tag):
        it = ListBoxItem()
        it.Content = text
        it.Tag = tag
        return it

    @staticmethod
    def select_combo_tag(combo, tag):
        for i in range(combo.Items.Count):
            if combo.Items[i].Tag == tag:
                combo.SelectedIndex = i
                return
        if combo.Items.Count:
            combo.SelectedIndex = 0

    @staticmethod
    def combo_tag(combo):
        it = combo.SelectedItem
        return it.Tag if it is not None else None

    @staticmethod
    def selected_tags(listbox):
        return [it.Tag for it in listbox.SelectedItems]

    def refresh_log(self):
        self.c["txtLog"].Text = u"\n".join(STATE["log"][-200:]) or \
            u"Ready. Pick fixtures on the first tab, or manage existing circuits on the others."
        self.c["txtLog"].ScrollToEnd()

    def save_state(self):
        STATE["tab"] = self.c["tabs"].SelectedIndex
        STATE["create_panel"] = self.combo_tag(self.c["cmbCreatePanel"])
        STATE["skip"] = bool(self.c["chkSkip"].IsChecked)
        STATE["filter_panel"] = self.combo_tag(self.c["cmbFilterPanel"])
        STATE["search"] = self.c["txtSearch"].Text or u""
        STATE["move_panel"] = self.combo_tag(self.c["cmbMovePanel"])
        STATE["edit_circuit"] = self.combo_tag(self.c["cmbEditCircuit"])
        STATE["preselect"] = set(self.selected_tags(self.c["lstCircuits"]))

    def close_for(self, pending):
        self.save_state()
        STATE["pending"] = pending
        self.win.Close()

    # ------------------------------------------------------------- fill lists
    def fill_panel_combos(self):
        self.panels = all_panels()
        cp, fp, mp = self.c["cmbCreatePanel"], self.c["cmbFilterPanel"], self.c["cmbMovePanel"]
        cp.Items.Clear(); fp.Items.Clear(); mp.Items.Clear()
        cp.Items.Add(self.combo_item(u"(no panel - leave unassigned)", None))
        fp.Items.Add(self.combo_item(u"(all circuits)", "__all__"))
        fp.Items.Add(self.combo_item(u"(circuits with no panel)", "__none__"))
        for p in self.panels:
            pid = eid_val(p.Id)
            name = panel_name(p)
            cp.Items.Add(self.combo_item(name, pid))
            fp.Items.Add(self.combo_item(name, pid))
            mp.Items.Add(self.combo_item(name, pid))
        self.select_combo_tag(cp, STATE["create_panel"])
        self.select_combo_tag(fp, STATE["filter_panel"])
        self.select_combo_tag(mp, STATE["move_panel"])
        self.c["txtSearch"].Text = STATE["search"]

    def fill_fixtures(self):
        lst = self.c["lstFixtures"]
        lst.Items.Clear()
        alive = []
        for v in STATE["fixtures"]:
            el = get_el(v)
            if el is None:
                continue
            alive.append(v)
            txt = element_label(el)
            ps = power_systems(el)
            if ps:
                txt += u"   * on circuit {0}".format(
                    u", ".join((s.PanelName or u"?") + u"/" + (s.CircuitNumber or u"-") for s in ps))
            lst.Items.Add(self.list_item(txt, v))
        STATE["fixtures"] = alive
        self.c["lblFixtures"].Text = u"Fixtures to circuit ({0})".format(len(alive))

    def fill_circuits(self, keep_selection=None):
        lst = self.c["lstCircuits"]
        if keep_selection is None:
            keep_selection = set(self.selected_tags(lst)) | STATE["preselect"]
        STATE["preselect"] = set()
        filt = self.combo_tag(self.c["cmbFilterPanel"])
        search = (self.c["txtSearch"].Text or u"").lower().strip()
        lst.Items.Clear()
        self.circuits = all_circuits()
        shown = 0
        for c in self.circuits:
            if filt == "__none__" and c.BaseEquipment is not None:
                continue
            if filt not in (None, "__all__", "__none__"):
                if c.BaseEquipment is None or eid_val(c.BaseEquipment.Id) != filt:
                    continue
            label = circuit_label(c)
            if search and search not in label.lower():
                continue
            cid = eid_val(c.Id)
            it = self.list_item(label, cid)
            lst.Items.Add(it)
            if cid in keep_selection:
                it.IsSelected = True
            shown += 1
        self.c["lblCircuits"].Text = u"Circuits  ({0} shown of {1})".format(shown, len(self.circuits))
        self.update_sel_count()

    def fill_edit_combo(self):
        cmb = self.c["cmbEditCircuit"]
        cmb.Items.Clear()
        for c in all_circuits():
            cmb.Items.Add(self.combo_item(circuit_label(c), eid_val(c.Id)))
        self.select_combo_tag(cmb, STATE["edit_circuit"])
        self.fill_edit_elems()

    def fill_edit_elems(self):
        lst = self.c["lstEditElems"]
        lst.Items.Clear()
        cid = self.combo_tag(self.c["cmbEditCircuit"])
        c = get_el(cid) if cid is not None else None
        if c is None:
            self.c["lblEditElems"].Text = u"No circuit selected"
            return
        n = 0
        for e in c.Elements:
            lst.Items.Add(self.list_item(element_label(e), eid_val(e.Id)))
            n += 1
        self.c["lblEditElems"].Text = u"{0} element(s) on this circuit".format(n)

    def refresh_all(self):
        keep = set(self.selected_tags(self.c["lstCircuits"]))
        STATE["edit_circuit"] = self.combo_tag(self.c["cmbEditCircuit"])
        self.fill_fixtures()
        self.fill_circuits(keep)
        self.fill_edit_combo()
        self.refresh_log()

    def update_sel_count(self):
        n = self.c["lstCircuits"].SelectedItems.Count
        self.c["lblSelCount"].Text = u"{0} circuit(s) selected".format(n)

    def selected_circuits(self):
        out = []
        for v in self.selected_tags(self.c["lstCircuits"]):
            c = get_el(v)
            if c is not None:
                out.append(c)
        return out

    # ------------------------------------------------------------ TAB 1 events
    def on_pick_fixtures(self, s, e):
        self.close_for("pick_fixtures")

    def on_remove_fix(self, s, e):
        rem = set(self.selected_tags(self.c["lstFixtures"]))
        STATE["fixtures"] = [v for v in STATE["fixtures"] if v not in rem]
        self.fill_fixtures()

    def on_clear_fix(self, s, e):
        STATE["fixtures"] = []
        self.fill_fixtures()

    def on_create(self, s, e):
        if not STATE["fixtures"]:
            log(u"Create: the list is empty - pick fixtures first.")
            self.refresh_log()
            return
        skip = bool(self.c["chkSkip"].IsChecked)
        pid = self.combo_tag(self.c["cmbCreatePanel"])
        panel = get_el(pid) if pid is not None else None
        made, skipped, failed, no_panel = 0, 0, 0, 0
        with Tx("Create circuits"):
            for v in STATE["fixtures"]:
                el = get_el(v)
                if el is None:
                    continue
                if skip and power_systems(el):
                    skipped += 1
                    continue
                ids = List[DB.ElementId]()
                ids.Add(el.Id)
                try:
                    sys_ = ElectricalSystem.Create(doc, ids, ElectricalSystemType.PowerCircuit)
                except Exception as ex:
                    failed += 1
                    log(u"  x {0}: {1}".format(element_label(el), ex))
                    continue
                if sys_ is None:
                    failed += 1
                    log(u"  x {0}: Revit returned no circuit".format(element_label(el)))
                    continue
                made += 1
                if panel is not None:
                    try:
                        sys_.SelectPanel(panel)
                    except Exception as ex:
                        no_panel += 1
                        log(u"  ! circuit for id {0} not assigned to {1}: {2}".format(
                            v, panel_name(panel), ex))
        msg = u"Create: {0} circuit(s) created".format(made)
        if panel is not None:
            msg += u" on {0}".format(panel_name(panel))
        if skipped:
            msg += u", {0} skipped (already circuited)".format(skipped)
        if failed:
            msg += u", {0} failed".format(failed)
        if no_panel:
            msg += u", {0} left without panel".format(no_panel)
        log(msg + u".")
        self.refresh_all()

    # ------------------------------------------------------------ TAB 2 events
    def on_filter_changed(self, s, e):
        if self.c.get("lstCircuits") is not None and hasattr(self, "circuits"):
            self.fill_circuits()

    def on_circuit_sel_changed(self, s, e):
        self.update_sel_count()

    def on_sel_all(self, s, e):
        self.c["lstCircuits"].SelectAll()
        self.update_sel_count()

    def on_sel_none(self, s, e):
        self.c["lstCircuits"].UnselectAll()
        self.update_sel_count()

    def on_pick_circuits(self, s, e):
        self.close_for("pick_circuits")

    def on_refresh(self, s, e):
        self.fill_panel_combos()
        self.refresh_all()

    def on_move(self, s, e):
        circuits = self.selected_circuits()
        pid = self.combo_tag(self.c["cmbMovePanel"])
        panel = get_el(pid) if pid is not None else None
        if not circuits or panel is None:
            log(u"Move: select circuits and a target panel first.")
            self.refresh_log()
            return
        ok, bad = 0, 0
        with Tx("Move circuits"):
            for c in circuits:
                label = circuit_label(c)
                try:
                    c.SelectPanel(panel)
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(label, ex))
        log(u"Move: {0} circuit(s) moved to {1}{2}.".format(
            ok, panel_name(panel), u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    def on_disconnect(self, s, e):
        circuits = self.selected_circuits()
        if not circuits:
            log(u"Disconnect: select circuits first.")
            self.refresh_log()
            return
        ok, bad = 0, 0
        with Tx("Disconnect circuits"):
            for c in circuits:
                if c.BaseEquipment is None:
                    continue
                try:
                    c.DisconnectPanel()
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(circuit_label(c), ex))
        log(u"Disconnect: {0} circuit(s) disconnected from their panel{1}.".format(
            ok, u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    def on_edit_selected(self, s, e):
        tags = self.selected_tags(self.c["lstCircuits"])
        if not tags:
            return
        self.select_combo_tag(self.c["cmbEditCircuit"], tags[0])
        self.fill_edit_elems()
        self.c["tabs"].SelectedIndex = 2

    def on_delete(self, s, e):
        circuits = self.selected_circuits()
        if not circuits:
            log(u"Delete: select circuits first.")
            self.refresh_log()
            return
        from System.Windows import MessageBox, MessageBoxButton, MessageBoxResult
        ans = MessageBox.Show(
            u"Delete {0} circuit(s)?\n\nThe fixtures stay in the model, only the "
            u"circuits are removed. You can Undo this in Revit.".format(len(circuits)),
            u"Circuit Manager Pro", MessageBoxButton.YesNo)
        if ans != MessageBoxResult.Yes:
            return
        ok, bad = 0, 0
        with Tx("Delete circuits"):
            for c in circuits:
                label = circuit_label(c)
                try:
                    doc.Delete(c.Id)
                    ok += 1
                except Exception as ex:
                    bad += 1
                    log(u"  x {0}: {1}".format(label, ex))
        log(u"Delete: {0} circuit(s) deleted{1}.".format(
            ok, u", {0} failed".format(bad) if bad else u""))
        self.refresh_all()

    # ------------------------------------------------------------ TAB 3 events
    def on_edit_circuit_changed(self, s, e):
        self.fill_edit_elems()

    def on_add_elems(self, s, e):
        if self.combo_tag(self.c["cmbEditCircuit"]) is None:
            log(u"Add: choose a circuit first.")
            self.refresh_log()
            return
        self.close_for("add_elems")

    def on_remove_elems(self, s, e):
        cid = self.combo_tag(self.c["cmbEditCircuit"])
        c = get_el(cid) if cid is not None else None
        elems = [get_el(v) for v in self.selected_tags(self.c["lstEditElems"])]
        elems = [x for x in elems if x is not None]
        if c is None or not elems:
            log(u"Remove: choose a circuit and select elements in the list.")
            self.refresh_log()
            return
        try:
            with Tx("Remove elements from circuit"):
                c.RemoveFromCircuit(make_set(elems))
            log(u"Remove: {0} element(s) removed from {1}.".format(len(elems), circuit_label(c)))
        except Exception as ex:
            log(u"Remove failed: {0}".format(ex))
        self.refresh_all()

    def on_close(self, s, e):
        self.save_state()
        STATE["pending"] = None
        self.win.Close()

    def show(self):
        self.win.ShowDialog()


# =============================================================================
# Pick actions (run while the dialog is closed, then it reopens)
# =============================================================================
def do_pick_fixtures():
    picked = pick_elements("Pick fixtures / devices to circuit, then click Finish")
    have = set(STATE["fixtures"])
    added, ignored = 0, 0
    for el in picked:
        if not is_circuitable(el):
            ignored += 1
            continue
        v = eid_val(el.Id)
        if v not in have:
            STATE["fixtures"].append(v)
            have.add(v)
            added += 1
    log(u"Pick: {0} element(s) added to the list{1}.".format(
        added, u", {0} ignored (no electrical connector)".format(ignored) if ignored else u""))
    STATE["tab"] = 0


def do_pick_circuits():
    picked = pick_elements("Pick elements - their circuits get selected - then click Finish")
    ids = set()
    for el in picked:
        if isinstance(el, ElectricalSystem):
            ids.add(eid_val(el.Id))
            continue
        for s in element_systems(el):
            ids.add(eid_val(s.Id))
    STATE["preselect"] = ids
    STATE["filter_panel"] = "__all__"
    STATE["search"] = u""
    log(u"Select: {0} circuit(s) found on the picked elements.".format(len(ids)))
    STATE["tab"] = 1


def do_add_elems():
    c = get_el(STATE["edit_circuit"]) if STATE["edit_circuit"] is not None else None
    STATE["tab"] = 2
    if c is None:
        return
    picked = [el for el in pick_elements("Pick elements to ADD to the circuit, then click Finish")
              if is_circuitable(el)]
    if not picked:
        log(u"Add: nothing picked.")
        return
    try:
        with Tx("Add elements to circuit"):
            c.AddToCircuit(make_set(picked))
        log(u"Add: {0} element(s) added to {1}.".format(len(picked), circuit_label(c)))
    except Exception as ex:
        log(u"Add failed: {0}".format(ex))


# =============================================================================
# Main
# =============================================================================
def main():
    if doc.IsFamilyDocument:
        print("Circuit Manager Pro works in a project, not in the Family Editor.")
        return
    actions = {
        "pick_fixtures": do_pick_fixtures,
        "pick_circuits": do_pick_circuits,
        "add_elems": do_add_elems,
    }
    while True:
        STATE["pending"] = None
        try:
            CircuitManagerWindow().show()
        except Exception as ex:
            log(u"Dialog error: {0}".format(ex))
            break
        action = actions.get(STATE["pending"])
        if action is None:
            break
        action()

    if STATE["log"]:
        print("Circuit Manager Pro - action log")
        print("-" * 60)
        for line in STATE["log"]:
            print(line)
    else:
        print("Circuit Manager Pro - no changes made.")


main()
