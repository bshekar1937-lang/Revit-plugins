// -----------------------------------------------------------------------------
// Geometry and units for the cable route core.
//
// Nothing in the Core namespace references the Revit API, so it can be built
// and tested on its own. The Revit layer converts XYZ to Pt on the way in and
// Pt back to XYZ on the way out.
//
// Lengths are kept in Revit's internal unit (decimal feet) all the way
// through, exactly as the model stores them, and converted only for display.
// -----------------------------------------------------------------------------

using System;
using System.Collections.Generic;

namespace PanelCableRoute.Core
{
    /// <summary>A point in model space, in decimal feet.</summary>
    public struct Pt
    {
        public readonly double X;
        public readonly double Y;
        public readonly double Z;

        public Pt(double x, double y, double z)
        {
            X = x;
            Y = y;
            Z = z;
        }

        public double DistanceTo(Pt other)
        {
            double dx = X - other.X;
            double dy = Y - other.Y;
            double dz = Z - other.Z;
            return Math.Sqrt(dx * dx + dy * dy + dz * dz);
        }

        public Pt Minus(Pt other)
        {
            return new Pt(X - other.X, Y - other.Y, Z - other.Z);
        }

        public Pt Plus(Pt other)
        {
            return new Pt(X + other.X, Y + other.Y, Z + other.Z);
        }

        public Pt Scaled(double factor)
        {
            return new Pt(X * factor, Y * factor, Z * factor);
        }

        public double Length
        {
            get { return Math.Sqrt(X * X + Y * Y + Z * Z); }
        }

        /// <summary>Unit vector, or null when the vector has no length.</summary>
        public Pt? Unit()
        {
            double length = Length;
            if (length <= 1e-12)
            {
                return null;
            }
            return new Pt(X / length, Y / length, Z / length);
        }

        public Pt Cross(Pt other)
        {
            return new Pt(
                Y * other.Z - Z * other.Y,
                Z * other.X - X * other.Z,
                X * other.Y - Y * other.X);
        }

        public double Dot(Pt other)
        {
            return X * other.X + Y * other.Y + Z * other.Z;
        }

        public override string ToString()
        {
            return string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "({0:0.###}, {1:0.###}, {2:0.###})", X, Y, Z);
        }
    }

    /// <summary>Conversions between feet, millimetres and metres.</summary>
    public static class Units
    {
        public const double FeetToMetresFactor = 0.3048;
        public const double MillimetresPerFoot = 304.8;

        public static double MmToFeet(double millimetres)
        {
            return millimetres / MillimetresPerFoot;
        }

        public static double FeetToMm(double value)
        {
            return value * MillimetresPerFoot;
        }

        public static double FeetToMetres(double value)
        {
            return value * FeetToMetresFactor;
        }

        public static double MetresToFeet(double metres)
        {
            return metres / FeetToMetresFactor;
        }
    }

    /// <summary>The geometry the router and the 3D preview need.</summary>
    public static class Geometry
    {
        public const double Epsilon = 1e-9;

        /// <summary>
        /// Across, along and up: how a cable actually gets from one point to
        /// another, rather than straight through the structure.
        /// </summary>
        public static double OrthogonalDistance(Pt start, Pt finish)
        {
            return Math.Abs(start.X - finish.X)
                 + Math.Abs(start.Y - finish.Y)
                 + Math.Abs(start.Z - finish.Z);
        }

        /// <summary>The right-angled run from start to finish: up, across, along.</summary>
        public static List<Pt> OrthogonalPoints(Pt start, Pt finish)
        {
            List<Pt> points = new List<Pt>(4);
            points.Add(start);
            points.Add(new Pt(start.X, start.Y, finish.Z));
            points.Add(new Pt(finish.X, start.Y, finish.Z));
            points.Add(finish);
            return points;
        }

        /// <summary>
        /// The point on segment a-b nearest to p, clamped to the segment, and
        /// how far along the segment it is.
        /// </summary>
        public static void ProjectOntoSegment(
            Pt a, Pt b, Pt p, out Pt point, out double distanceAlong)
        {
            Pt direction = b.Minus(a);
            double length = direction.Length;
            if (length <= Epsilon)
            {
                point = a;
                distanceAlong = 0.0;
                return;
            }
            double t = p.Minus(a).Dot(direction) / (length * length);
            if (t < 0.0)
            {
                t = 0.0;
            }
            else if (t > 1.0)
            {
                t = 1.0;
            }
            point = a.Plus(direction.Scaled(t));
            distanceAlong = t * length;
        }

        /// <summary>A tray that rises with no run in plan is a riser.</summary>
        public static bool IsVertical(Pt start, Pt end, double tolerance)
        {
            double planLength = Math.Sqrt(
                (end.X - start.X) * (end.X - start.X) +
                (end.Y - start.Y) * (end.Y - start.Y));
            return Math.Abs(end.Z - start.Z) > tolerance
                && planLength <= tolerance;
        }

        /// <summary>
        /// The eight corners of a box running start to finish, where normal
        /// and up are the half-width and half-height of the cross-section.
        /// Corners 0-3 are the start face, 4-7 the finish face.
        /// </summary>
        public static Pt[] PrismCorners(Pt start, Pt end, Pt normal, Pt up)
        {
            int[] normalSigns = new int[] { -1, 1, 1, -1 };
            int[] upSigns = new int[] { -1, -1, 1, 1 };
            Pt[] corners = new Pt[8];
            int index = 0;
            for (int face = 0; face < 2; face++)
            {
                Pt origin = face == 0 ? start : end;
                for (int corner = 0; corner < 4; corner++)
                {
                    corners[index++] = new Pt(
                        origin.X + normalSigns[corner] * normal.X
                                 + upSigns[corner] * up.X,
                        origin.Y + normalSigns[corner] * normal.Y
                                 + upSigns[corner] * up.Y,
                        origin.Z + normalSigns[corner] * normal.Z
                                 + upSigns[corner] * up.Z);
                }
            }
            return corners;
        }

        /// <summary>The eight corners of an axis-aligned box.</summary>
        public static Pt[] BoxCorners(Pt low, Pt high)
        {
            double middleY = (low.Y + high.Y) / 2.0;
            double middleZ = (low.Z + high.Z) / 2.0;
            return PrismCorners(
                new Pt(low.X, middleY, middleZ),
                new Pt(high.X, middleY, middleZ),
                new Pt(0.0, (high.Y - low.Y) / 2.0, 0.0),
                new Pt(0.0, 0.0, (high.Z - low.Z) / 2.0));
        }

        /// <summary>
        /// A square tube from start to end, for drawing a line with some
        /// thickness. Returns null when the two points coincide.
        /// </summary>
        public static Pt[] TubeCorners(
            Pt start, Pt end, double halfWidth, double halfHeight)
        {
            Pt? direction = end.Minus(start).Unit();
            if (!direction.HasValue)
            {
                return null;
            }
            Pt along = direction.Value;
            Pt helper = Math.Abs(along.Z) < 0.9
                ? new Pt(0.0, 0.0, 1.0)
                : new Pt(1.0, 0.0, 0.0);
            Pt? normal = along.Cross(helper).Unit();
            if (!normal.HasValue)
            {
                return null;
            }
            Pt? up = normal.Value.Cross(along).Unit();
            Pt upVector = up.HasValue ? up.Value : new Pt(0.0, 0.0, 1.0);
            return PrismCorners(
                start, end,
                normal.Value.Scaled(halfWidth),
                upVector.Scaled(halfHeight));
        }

        /// <summary>
        /// The cross-section of a tray: half-width across the run in plan,
        /// half-height perpendicular to both. Returns false for a tray with
        /// no length.
        /// </summary>
        public static bool TraySection(
            Pt start, Pt end, double halfWidth, double halfHeight,
            out Pt normal, out Pt up)
        {
            normal = new Pt(0.0, 0.0, 0.0);
            up = new Pt(0.0, 0.0, 0.0);
            Pt? direction = end.Minus(start).Unit();
            if (!direction.HasValue)
            {
                return false;
            }
            Pt along = direction.Value;
            double planLength = Math.Sqrt(along.X * along.X + along.Y * along.Y);
            Pt across = planLength > 1e-6
                ? new Pt(along.Y / planLength, -along.X / planLength, 0.0)
                : new Pt(1.0, 0.0, 0.0);
            Pt? vertical = across.Cross(along).Unit();
            Pt upVector = vertical.HasValue
                ? vertical.Value
                : new Pt(0.0, 0.0, 1.0);
            normal = across.Scaled(halfWidth);
            up = upVector.Scaled(halfHeight);
            return true;
        }
    }
}
