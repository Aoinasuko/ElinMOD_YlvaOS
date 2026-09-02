using System;

namespace YlvaOS
{
    internal sealed class YlvaUserException : Exception
    {
        public YlvaUserException(string message)
            : base(message)
        {
        }
    }
}
