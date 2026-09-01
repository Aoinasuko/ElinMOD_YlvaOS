using System;
using System.Collections.Generic;
using System.Text;

namespace YlvaOS
{
    internal sealed class YlvaTerminalBuffer
    {
        public const int DefaultColumns = 140;
        public const int DefaultRows = 32;

        private const int TabWidth = 8;
        private const int MaxEscapeLength = 96;
        private const int MaxOscLength = 512;
        private const string HostOscPrefix = "777;ylvaos;";

        private readonly List<string> lines;
        private readonly Action<string> hostCommand;
        private readonly Action<string> terminalResponse;
        private readonly StringBuilder escapeBuffer = new StringBuilder(32);
        private readonly StringBuilder oscBuffer = new StringBuilder(128);
        private readonly int rows;
        private readonly int columns;

        private EscapeMode escapeMode;
        private int cursorRow;
        private int cursorColumn;
        private int savedRow;
        private int savedColumn;

        public YlvaTerminalBuffer(List<string> lines, int rows, int columns, Action<string> hostCommand, Action<string> terminalResponse)
        {
            if (lines == null)
            {
                throw new ArgumentNullException("lines");
            }

            this.lines = lines;
            this.rows = Math.Max(1, rows);
            this.columns = Math.Max(20, columns);
            this.hostCommand = hostCommand;
            this.terminalResponse = terminalResponse;
            TrimLines();
            cursorRow = Math.Max(0, this.lines.Count - 1);
            cursorColumn = this.lines.Count > 0 ? Math.Min(this.columns - 1, this.lines[cursorRow].Length) : 0;
        }

        private enum EscapeMode
        {
            None,
            Escape,
            Csi,
            Osc,
            OscEscape,
            IgnoreOne
        }

        public void AppendChunk(string text)
        {
            if (string.IsNullOrEmpty(text))
            {
                return;
            }

            foreach (char ch in text)
            {
                Consume(ch);
            }

            TrimLines();
        }

        public void AppendPlainLine(string line)
        {
            ResetControlSequence();
            if (line == null)
            {
                line = string.Empty;
            }

            if (line.Length == 0)
            {
                PushLine(string.Empty);
                return;
            }

            int offset = 0;
            while (offset < line.Length)
            {
                int length = Math.Min(columns, line.Length - offset);
                PushLine(line.Substring(offset, length));
                offset += length;
            }
        }

        public void Clear()
        {
            lines.Clear();
            cursorRow = 0;
            cursorColumn = 0;
            savedRow = 0;
            savedColumn = 0;
            ResetControlSequence();
        }

        public List<string> GetVisibleLines(int maxLines, bool showCursor)
        {
            int count = Math.Max(1, maxLines);
            List<string> result = new List<string>(count);
            int start = Math.Max(0, lines.Count - count);
            for (int i = start; i < lines.Count; i++)
            {
                result.Add(lines[i]);
            }

            while (result.Count < count)
            {
                result.Add(string.Empty);
            }

            if (showCursor)
            {
                int visibleCursorRow = cursorRow - start;
                if (visibleCursorRow >= 0 && visibleCursorRow < result.Count)
                {
                    result[visibleCursorRow] = RenderCursor(result[visibleCursorRow]);
                }
            }

            return result;
        }

        public void ResetTransientState()
        {
            ResetControlSequence();
        }

        private void Consume(char ch)
        {
            if (escapeMode != EscapeMode.None)
            {
                ConsumeEscape(ch);
                return;
            }

            if (ch == '\u001b')
            {
                escapeMode = EscapeMode.Escape;
                escapeBuffer.Length = 0;
                return;
            }

            if (ch == '\u009b')
            {
                escapeMode = EscapeMode.Csi;
                escapeBuffer.Length = 0;
                return;
            }

            switch (ch)
            {
                case '\r':
                    EnsureLine(cursorRow);
                    cursorColumn = 0;
                    return;
                case '\n':
                    NewLine();
                    return;
                case '\b':
                    if (cursorColumn > 0)
                    {
                        cursorColumn--;
                    }

                    return;
                case '\t':
                    PrintTab();
                    return;
                case '\f':
                    Clear();
                    return;
            }

            if (char.IsControl(ch))
            {
                return;
            }

            Print(ch);
        }

        private void ConsumeEscape(char ch)
        {
            switch (escapeMode)
            {
                case EscapeMode.Escape:
                    ConsumeEscapeStarter(ch);
                    return;
                case EscapeMode.Csi:
                    ConsumeCsi(ch);
                    return;
                case EscapeMode.Osc:
                    ConsumeOsc(ch);
                    return;
                case EscapeMode.OscEscape:
                    if (ch == '\\')
                    {
                        FinishOsc();
                    }
                    else
                    {
                        AppendOsc('\u001b');
                        AppendOsc(ch);
                        escapeMode = EscapeMode.Osc;
                    }

                    return;
                case EscapeMode.IgnoreOne:
                    escapeMode = EscapeMode.None;
                    return;
                default:
                    escapeMode = EscapeMode.None;
                    return;
            }
        }

        private void ConsumeEscapeStarter(char ch)
        {
            if (ch == '[')
            {
                escapeMode = EscapeMode.Csi;
                escapeBuffer.Length = 0;
                return;
            }

            if (ch == ']')
            {
                escapeMode = EscapeMode.Osc;
                oscBuffer.Length = 0;
                return;
            }

            switch (ch)
            {
                case 'c':
                    Clear();
                    return;
                case 'D':
                case 'E':
                    NewLine();
                    break;
                case 'M':
                    ReverseIndex();
                    break;
                case '7':
                    SaveCursor();
                    break;
                case '8':
                    RestoreCursor();
                    break;
            }

            if (ch == '(' || ch == ')' || ch == '*' || ch == '+' || ch == '-' || ch == '.' || ch == '/' || ch == '#')
            {
                escapeMode = EscapeMode.IgnoreOne;
                return;
            }

            escapeMode = EscapeMode.None;
            escapeBuffer.Length = 0;
        }

        private void ConsumeCsi(char ch)
        {
            if ((ch >= '0' && ch <= '?') || (ch >= ' ' && ch <= '/'))
            {
                if (escapeBuffer.Length < MaxEscapeLength)
                {
                    escapeBuffer.Append(ch);
                }

                return;
            }

            ApplyCsi(ch, escapeBuffer.ToString());
            escapeMode = EscapeMode.None;
            escapeBuffer.Length = 0;
        }

        private void ConsumeOsc(char ch)
        {
            if (ch == '\a' || ch == '\u009c')
            {
                FinishOsc();
                return;
            }

            if (ch == '\u001b')
            {
                escapeMode = EscapeMode.OscEscape;
                return;
            }

            AppendOsc(ch);
        }

        private void FinishOsc()
        {
            string osc = oscBuffer.ToString();
            if (osc.StartsWith(HostOscPrefix, StringComparison.Ordinal) && hostCommand != null)
            {
                hostCommand(osc.Substring(HostOscPrefix.Length));
            }

            oscBuffer.Length = 0;
            escapeMode = EscapeMode.None;
        }

        private void AppendOsc(char ch)
        {
            if (oscBuffer.Length < MaxOscLength)
            {
                oscBuffer.Append(ch);
            }
        }

        private void ApplyCsi(char finalChar, string parameters)
        {
            switch (finalChar)
            {
                case 'A':
                    MoveCursor(-Math.Max(1, ParseCsiParameter(parameters, 0, 1)), 0);
                    return;
                case 'B':
                    MoveCursor(Math.Max(1, ParseCsiParameter(parameters, 0, 1)), 0);
                    return;
                case 'C':
                    MoveCursor(0, Math.Max(1, ParseCsiParameter(parameters, 0, 1)));
                    return;
                case 'D':
                    MoveCursor(0, -Math.Max(1, ParseCsiParameter(parameters, 0, 1)));
                    return;
                case 'E':
                    MoveTo(cursorRow + Math.Max(1, ParseCsiParameter(parameters, 0, 1)), 0);
                    return;
                case 'F':
                    MoveTo(cursorRow - Math.Max(1, ParseCsiParameter(parameters, 0, 1)), 0);
                    return;
                case 'G':
                    MoveTo(cursorRow, ParseCsiParameter(parameters, 0, 1) - 1);
                    return;
                case 'H':
                case 'f':
                    MoveTo(ParseCsiParameter(parameters, 0, 1) - 1, ParseCsiParameter(parameters, 1, 1) - 1);
                    return;
                case 'J':
                    EraseDisplay(ParseCsiParameter(parameters, 0, 0));
                    return;
                case 'K':
                    EraseLine(ParseCsiParameter(parameters, 0, 0));
                    return;
                case 'd':
                    MoveTo(ParseCsiParameter(parameters, 0, 1) - 1, cursorColumn);
                    return;
                case 'm':
                    return;
                case 'n':
                    ReportDeviceStatus(parameters);
                    return;
                case 's':
                    SaveCursor();
                    return;
                case 'u':
                    RestoreCursor();
                    return;
                case 'h':
                case 'l':
                    if (parameters.IndexOf("1049", StringComparison.Ordinal) >= 0 || parameters.IndexOf("47", StringComparison.Ordinal) >= 0)
                    {
                        Clear();
                    }

                    return;
            }
        }

        private void PrintTab()
        {
            int spaces = TabWidth - (cursorColumn % TabWidth);
            for (int i = 0; i < spaces; i++)
            {
                Print(' ');
            }
        }

        private void Print(char ch)
        {
            EnsureLine(cursorRow);
            if (cursorColumn >= columns)
            {
                NewLine();
            }

            EnsureLine(cursorRow);
            string line = lines[cursorRow];
            if (cursorColumn < line.Length)
            {
                StringBuilder builder = new StringBuilder(line);
                builder[cursorColumn] = ch;
                lines[cursorRow] = builder.ToString();
            }
            else
            {
                if (cursorColumn > line.Length)
                {
                    line += new string(' ', cursorColumn - line.Length);
                }

                lines[cursorRow] = line + ch;
            }

            cursorColumn++;
            if (cursorColumn >= columns)
            {
                NewLine();
            }
        }

        private void NewLine()
        {
            EnsureLine(cursorRow);
            if (cursorRow >= rows - 1)
            {
                if (lines.Count == 0)
                {
                    lines.Add(string.Empty);
                }
                else
                {
                    lines.RemoveAt(0);
                    lines.Add(string.Empty);
                }

                cursorRow = rows - 1;
            }
            else
            {
                cursorRow++;
                EnsureLine(cursorRow);
            }

            cursorColumn = 0;
            TrimLines();
        }

        private void ReverseIndex()
        {
            if (cursorRow > 0)
            {
                cursorRow--;
                return;
            }

            lines.Insert(0, string.Empty);
            TrimLinesFromEnd();
        }

        private void PushLine(string line)
        {
            if (line == null)
            {
                line = string.Empty;
            }

            lines.Add(line.Length > columns ? line.Substring(0, columns) : line);
            TrimLines();
            cursorRow = Math.Max(0, lines.Count - 1);
            cursorColumn = 0;
        }

        private void EnsureLine(int row)
        {
            if (row < 0)
            {
                row = 0;
            }

            while (lines.Count <= row)
            {
                lines.Add(string.Empty);
            }

            TrimLines();
        }

        private void MoveCursor(int rowDelta, int columnDelta)
        {
            MoveTo(cursorRow + rowDelta, cursorColumn + columnDelta);
        }

        private void MoveTo(int row, int column)
        {
            cursorRow = Clamp(row, 0, rows - 1);
            cursorColumn = Clamp(column, 0, columns - 1);
            EnsureLine(cursorRow);
        }

        private void SaveCursor()
        {
            savedRow = cursorRow;
            savedColumn = cursorColumn;
        }

        private void RestoreCursor()
        {
            MoveTo(savedRow, savedColumn);
        }

        private void EraseDisplay(int mode)
        {
            EnsureLine(cursorRow);
            if (mode == 2 || mode == 3)
            {
                Clear();
                return;
            }

            if (mode == 1)
            {
                for (int i = 0; i < cursorRow && i < lines.Count; i++)
                {
                    lines[i] = string.Empty;
                }

                EraseLine(1);
                return;
            }

            EraseLine(0);
            for (int i = cursorRow + 1; i < lines.Count; i++)
            {
                lines[i] = string.Empty;
            }
        }

        private void EraseLine(int mode)
        {
            EnsureLine(cursorRow);
            string line = lines[cursorRow];

            if (mode == 2)
            {
                lines[cursorRow] = string.Empty;
                return;
            }

            if (mode == 1)
            {
                int count = Math.Min(line.Length, cursorColumn + 1);
                if (count <= 0)
                {
                    return;
                }

                StringBuilder builder = new StringBuilder(line);
                for (int i = 0; i < count; i++)
                {
                    builder[i] = ' ';
                }

                lines[cursorRow] = builder.ToString();
                return;
            }

            if (cursorColumn < line.Length)
            {
                lines[cursorRow] = line.Substring(0, cursorColumn);
            }
        }

        private void ReportDeviceStatus(string parameters)
        {
            if (ParseCsiParameter(parameters, 0, 0) == 6 && terminalResponse != null)
            {
                terminalResponse("\u001b[" + (cursorRow + 1) + ";" + (cursorColumn + 1) + "R");
            }
        }

        private string RenderCursor(string line)
        {
            int column = Clamp(cursorColumn, 0, columns - 1);
            if (line == null)
            {
                line = string.Empty;
            }

            if (column < line.Length)
            {
                StringBuilder builder = new StringBuilder(line);
                builder[column] = builder[column] == ' ' ? '_' : builder[column];
                return builder.ToString();
            }

            if (column > line.Length)
            {
                line += new string(' ', column - line.Length);
            }

            return line + "_";
        }

        private void ResetControlSequence()
        {
            escapeMode = EscapeMode.None;
            escapeBuffer.Length = 0;
            oscBuffer.Length = 0;
        }

        private void TrimLines()
        {
            while (lines.Count > rows)
            {
                lines.RemoveAt(0);
            }

            cursorRow = Clamp(cursorRow, 0, Math.Max(0, rows - 1));
        }

        private void TrimLinesFromEnd()
        {
            while (lines.Count > rows)
            {
                lines.RemoveAt(lines.Count - 1);
            }
        }

        private static int ParseCsiParameter(string parameters, int index, int defaultValue)
        {
            if (string.IsNullOrEmpty(parameters))
            {
                return defaultValue;
            }

            string[] parts = parameters.Split(';');
            if (index < 0 || index >= parts.Length)
            {
                return defaultValue;
            }

            StringBuilder digits = new StringBuilder();
            foreach (char ch in parts[index])
            {
                if (char.IsDigit(ch) || (digits.Length == 0 && ch == '-'))
                {
                    digits.Append(ch);
                }
            }

            int value;
            return int.TryParse(digits.ToString(), out value) ? value : defaultValue;
        }

        private static int Clamp(int value, int min, int max)
        {
            if (value < min)
            {
                return min;
            }

            if (value > max)
            {
                return max;
            }

            return value;
        }
    }
}
