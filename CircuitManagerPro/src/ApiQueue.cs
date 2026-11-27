using System;
using System.Collections.Generic;
using Autodesk.Revit.UI;

namespace CircuitManagerPro
{
    /// <summary>
    /// Runs the panel's jobs inside Revit's API context. A modeless window may
    /// only use the Revit API through an ExternalEvent; Revit calls Execute
    /// when it is idle (no command running).
    /// </summary>
    internal class ApiQueue : IExternalEventHandler
    {
        private readonly Queue<Action<UIApplication>> _jobs = new Queue<Action<UIApplication>>();
        public ExternalEvent Event;

        public void Post(Action<UIApplication> job)
        {
            lock (_jobs) _jobs.Enqueue(job);
            Event?.Raise();
        }

        public void Execute(UIApplication app)
        {
            while (true)
            {
                Action<UIApplication> job;
                lock (_jobs)
                {
                    if (_jobs.Count == 0) return;
                    job = _jobs.Dequeue();
                }
                try { job(app); }
                catch (Exception ex) { App.Window?.Log("Error: " + ex.Message); }
            }
        }

        public string GetName() => "Circuit Manager Pro";
    }
}
